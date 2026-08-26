"""
EPUB 翻译工具集 - 使用 Toolsets 方式
"""

import base64
import re
from collections import Counter
from typing import Dict, List, Optional, Set

from bs4 import BeautifulSoup
from ebooklib import epub
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from .cache_manager import CacheManager
from .epub_tools import EpubTools
from .logger import ConsoleLevel, get_logger
from .models import TranslationProgress
from .settings import MIN_BLOCK_TAG_RATIO, MIN_INLINE_TAG_RATIO, TRANSLATE_IMAGES

# 捕获标签名，以便按块级 / 内联分别计数。
# 匹配集合必须与旧的 `<[a-zA-Z/][^>]*>` 完全一致（都要求闭合 `>`、都同时数开
# 标签与闭标签、都不匹配 `<!--` 和 `<?xml`），否则历史日志里的 tags 就不可比了。
_TAG_NAME_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*>")

# 块级标签：与段落一一对应，少一个就是真的少一段内容，是漏译判定的硬指标。
# img 也算块级——丢一张图同样是内容缺失。
# 其余（a/em/span/strong/br/i/b/sub/sup/code/small/cite/q/abbr…）视为内联，
# 模型系统性地会吞掉它们，丢了只影响排版细节，不阻塞保存。
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "ul",
        "ol",
        "dl",
        "dt",
        "dd",
        "blockquote",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "td",
        "th",
        "section",
        "article",
        "aside",
        "header",
        "footer",
        "figure",
        "figcaption",
        "pre",
        "hr",
        "img",
    }
)


def collect_toc_titles(toc_items) -> List[str]:
    """按 book.toc 的递归顺序摊平所有标题。

    与 apply_toc_titles 必须严格同序：一个负责取、一个负责放，
    顺序不一致会让译文错位到别的条目上。
    """
    titles: List[str] = []
    for item in toc_items:
        if isinstance(item, epub.Link):
            titles.append(item.title)
        elif isinstance(item, tuple):
            section, children = item
            if isinstance(section, epub.Link):
                titles.append(section.title)
            titles.extend(collect_toc_titles(children))
    return titles


def apply_toc_titles(toc_items, titles_iter):
    """按顺序把标题写回目录结构，返回重建后的结构"""
    updated = []
    for item in toc_items:
        if isinstance(item, epub.Link):
            new_title = next(titles_iter, item.title)
            updated.append(epub.Link(item.href, new_title, item.uid))
        elif isinstance(item, tuple):
            section, children = item
            if isinstance(section, epub.Link):
                new_title = next(titles_iter, section.title)
                new_section = epub.Link(section.href, new_title, section.uid)
            else:
                new_section = section
            updated.append((new_section, apply_toc_titles(children, titles_iter)))
    return updated


def _count_tags(html: str, *, block_only: bool = False) -> int:
    """统计 HTML 标签数量（开标签与闭标签都算）。

    译文要求原样保留标签，因此标签数是比字符数更可靠的完整性信号：
    中文译文字符数天然比英文原文少一半左右，用字符数判断会大量误报。

    Args:
        block_only: 只数块级标签（见 _BLOCK_TAGS）。漏译的硬判定用这个口径——
            全标签口径会把"内联标签被吞"误判成"整段没译"。
    """
    tags = _TAG_NAME_RE.findall(html)
    if not block_only:
        return len(tags)
    return sum(1 for _, name in tags if name.lower() in _BLOCK_TAGS)


def _missing_tag_names(source: str, translated: str) -> Dict[str, int]:
    """返回译文相比原文少掉的标签 {标签名: 少几个}，按缺失数降序。

    只比开标签：闭标签是镜像，两种都数会让数字翻倍、把提示语搞糊。
    """

    def opening(html: str) -> Counter:
        return Counter(
            name.lower() for slash, name in _TAG_NAME_RE.findall(html) if not slash
        )

    missing = opening(source) - opening(translated)
    return dict(missing.most_common())


def _describe_missing(missing: Dict[str, int]) -> str:
    """把 {标签名: 少几个} 写成"5 个 <a>、2 个 <em>"，供提示语点名"""
    return "、".join(f"{count} 个 <{name}>" for name, count in missing.items())


class EpubContext:
    """EPUB 翻译上下文（作为 deps）"""

    def __init__(
        self,
        book: epub.EpubBook,
        target_language: str,
        cache_key: Optional[str],  # 可以为 None
        cache_manager: Optional[CacheManager],  # 可以为 None
        glossary: Dict[str, str],
    ):
        self.book = book
        self.target_language = target_language
        self.cache_key = cache_key
        self.cache_manager = cache_manager
        self.glossary = glossary
        self.source_language = EpubTools.get_default_language(book)
        self.chapters = EpubTools.get_all_chapters(book)
        self.images = EpubTools.get_all_images(book)
        # 各章节的原文分块，翻译期间只读。
        # 曾经用"取出即弹出"的队列，一旦某块的写入失败，这块原文就永久消失了：
        # 只能静默漏译，或者整章重来。改成按块号寻址后，未写入译文的块会被反复
        # 发放，直到真的存进来。拆成块级 run 之后这条依然是核心：
        # 每块一次独立 run，run 之间没有 message history，块号是唯一的定位手段。
        self.chapter_chunks: Dict[int, List[str]] = {}
        # 逐块译文 {章节索引: {块号(从 0 起): 译文}}。缺号即未完成；
        # 拼接时按块号排序，不依赖写入顺序。
        self.chunk_translations: Dict[int, Dict[int, str]] = {}
        # 每块已尝试的次数 {章节索引: {块号: 次数}}。
        # **绝对不能在 prepare_chapter 里重置**：否则 MAX_CHUNK_RETRIES 的 3 次
        # 块重试会乘上 MAX_CHAPTER_RETRIES 的 3 次章重试，变成每块 9 次请求，
        # 一个翻不动的坏块就能把整本书的预算吃光。计数跨章级重试累计，
        # 硬上限是"每块每进程 MAX_CHUNK_RETRIES + 1 次"。
        self.chunk_attempts: Dict[int, Dict[int, int]] = {}
        # 保存成功（且完整）的章节索引，未启用缓存时用它判断落盘情况
        self.saved_chapters: Set[int] = set()
        # 保存了但判定不完整的章节 {章节索引: 原因}，供编排层如实报告
        self.incomplete_chapters: Dict[int, str] = {}

    # ---------- 分块状态查询（供编排层与块级翻译共用） ----------

    def chunk_count(self, chapter_index: int) -> int:
        return len(self.chapter_chunks.get(chapter_index, []))

    def pending_chunks(self, chapter_index: int) -> List[int]:
        """返回该章仍未写入译文的块号（升序）"""
        stored = self.chunk_translations.get(chapter_index, {})
        return [
            i
            for i in range(self.chunk_count(chapter_index))
            if not stored.get(i, "").strip()
        ]

    def assembled_translation(self, chapter_index: int) -> str:
        """按块号顺序拼接全章译文（模型乱序写入也能还原正确顺序）"""
        stored = self.chunk_translations.get(chapter_index, {})
        return "".join(
            stored.get(i, "") for i in range(self.chunk_count(chapter_index))
        )

    def source_tag_count(self, chapter_index: int, *, block_only: bool = False) -> int:
        """全章原文的标签数（由分块实时统计，不额外维护一份状态）"""
        return sum(
            _count_tags(c, block_only=block_only)
            for c in self.chapter_chunks.get(chapter_index, [])
        )

    def thin_chunks(self, chapter_index: int) -> List[int]:
        """返回块级标签数明显少于原文的块号——真漏译（会触发重发和硬拦）

        只看块级标签：内联标签（脚注 <a>、<em> 等）被模型吞掉不影响正文完整性，
        按全标签口径算会把"完整译完"的块误判成漏译。
        """
        stored = self.chunk_translations.get(chapter_index, {})
        thin = []
        for i, source in enumerate(self.chapter_chunks.get(chapter_index, [])):
            expected = _count_tags(source, block_only=True)
            actual = _count_tags(stored.get(i, ""), block_only=True)
            if expected and actual < expected * MIN_BLOCK_TAG_RATIO:
                thin.append(i)
        return thin

    def attempts(self, chapter_index: int, chunk_index: int) -> int:
        """某块已经尝试过几次（跨章级重试累计，见 chunk_attempts）"""
        return self.chunk_attempts.get(chapter_index, {}).get(chunk_index, 0)

    def record_attempt(self, chapter_index: int, chunk_index: int) -> int:
        """给某块的尝试次数 +1，返回这是第几次尝试"""
        per_chapter = self.chunk_attempts.setdefault(chapter_index, {})
        per_chapter[chunk_index] = per_chapter.get(chunk_index, 0) + 1
        return per_chapter[chunk_index]

    def prepare_chapter(self, chapter_index: int) -> int:
        """切分章节内容并重置该章状态，返回分块数。

        切分由 Python 在翻译之前完成，不作为 Agent 工具暴露：
        模型重复调用切分会清空已攒的译文，导致永远保存不了。

        注意这里**不重置 chunk_attempts**：块级重试次数必须跨章级重试累计，
        理由见该字段的注释。

        Returns:
            分块数量；章节内容为空或解码失败时返回 0
        """
        self.chapter_chunks[chapter_index] = []
        self.chunk_translations[chapter_index] = {}
        self.incomplete_chapters.pop(chapter_index, None)

        chapter = self.chapters[chapter_index - 1]
        data = chapter.get_content()
        if not data:
            return 0

        try:
            content = data.decode("utf-8", errors="ignore")
        except Exception as e:
            get_logger().error(f"章节 {chapter_index} 解码失败: {type(e).__name__}")
            return 0

        chunks = EpubTools.split_html_content(content)
        self.chapter_chunks[chapter_index] = list(chunks)

        # 分块尺寸是定位"输出被截断"类失败的关键证据，逐块记录
        get_logger().chunks(
            chapter_index,
            [
                {
                    "tokens": EpubTools.count_tokens(c),
                    "chars": len(c),
                    "tags": _count_tags(c),
                }
                for c in chunks
            ],
        )
        return len(chunks)

    def reset_chapter(self, chapter_index: int) -> None:
        """清理某章节的所有中间状态（重试该章前调用）

        同样不动 chunk_attempts，理由见该字段的注释。
        """
        self.chapter_chunks.pop(chapter_index, None)
        self.chunk_translations.pop(chapter_index, None)
        self.incomplete_chapters.pop(chapter_index, None)


# 创建工具集
epub_toolset: FunctionToolset[EpubContext] = FunctionToolset()


@epub_toolset.tool
def get_book_info(ctx: RunContext[EpubContext]) -> str:
    """
    获取 EPUB 书籍基本信息

    返回书籍的标题、作者、语言、章节数等信息
    """
    logger = get_logger()
    logger.console("正在获取书籍信息...")
    logger.tool_call("get_book_info")
    book = ctx.deps.book
    title = book.get_metadata("DC", "title")
    author = book.get_metadata("DC", "creator")

    title_str = title[0][0] if title else "Unknown"
    author_str = author[0][0] if author else "Unknown"

    info = f"""\
书籍信息:
- 标题: {title_str}
- 作者: {author_str}
- 源语言: {ctx.deps.source_language}
- 目标语言: {ctx.deps.target_language}
- 章节数: {len(ctx.deps.chapters)}
- 图片数: {len(ctx.deps.images)}
"""
    return info


@epub_toolset.tool
def list_chapters(ctx: RunContext[EpubContext]) -> str:
    """
    列出所有章节

    返回章节列表，包括章节 ID 和标题
    """
    logger = get_logger()
    logger.console("正在查看章节信息...")
    logger.tool_call("list_chapters", f"{len(ctx.deps.chapters)} 章")
    chapters_info = []

    # 获取已完成章节列表
    completed_chapters = []
    if ctx.deps.cache_manager and ctx.deps.cache_key:
        progress = ctx.deps.cache_manager.load_progress(ctx.deps.cache_key)
        if progress:
            completed_chapters = progress.completed_chapters

    for idx, chapter in enumerate(ctx.deps.chapters, 1):
        chapter_id = chapter.get_id()
        chapter_name = chapter.get_name() or chapter_id

        # 检查是否已翻译
        status = "✓ 已翻译" if chapter_id in completed_chapters else "待翻译"

        chapters_info.append(f"{idx}. {chapter_name} ({chapter_id}) - {status}")

    return "\n".join(chapters_info)


@epub_toolset.tool
def update_glossary(ctx: RunContext[EpubContext], new_terms: Dict[str, str]) -> str:
    """
    更新术语表（专有名词翻译对照）

    Args:
        new_terms: 新的术语映射，格式 {"原文": "译文"}

    Returns:
        更新结果
    """
    ctx.deps.glossary.update(new_terms)
    get_logger().tool_call("update_glossary", f"写入 {len(new_terms)} 个术语")

    # 保存到缓存（如果启用）
    if ctx.deps.cache_manager and ctx.deps.cache_key:
        ctx.deps.cache_manager.update_progress(
            ctx.deps.cache_key, lambda progress: progress.glossary.update(new_terms)
        )

    return f"✓ 已更新 {len(new_terms)} 个术语"


@epub_toolset.tool
def get_glossary(ctx: RunContext[EpubContext]) -> str:
    """
    获取当前的术语表

    返回已记录的所有专有名词翻译对照
    """
    get_logger().tool_call("get_glossary", f"{len(ctx.deps.glossary)} 个术语")
    if not ctx.deps.glossary:
        return "术语表为空"

    items = [f"- {orig} → {trans}" for orig, trans in ctx.deps.glossary.items()]
    return "当前术语表:\n" + "\n".join(items)


@epub_toolset.tool
def get_translation_progress(ctx: RunContext[EpubContext]) -> str:
    """
    获取翻译进度

    返回已完成和待完成的章节统计
    """
    logger = get_logger()
    if not ctx.deps.cache_manager or not ctx.deps.cache_key:
        logger.tool_call("get_translation_progress", "缓存未启用")
        return "缓存未启用，无法获取进度"

    progress = ctx.deps.cache_manager.load_progress(ctx.deps.cache_key)

    if not progress:
        logger.tool_call("get_translation_progress", "无进度记录")
        return "无翻译进度记录"

    completed = len(progress.completed_chapters)
    total = progress.total_chapters
    failed = len(progress.failed_chapters)

    percentage = (completed / total * 100) if total > 0 else 0

    status = f"""翻译进度:
- 总章节数: {total}
- 已完成: {completed} ({percentage:.1f}%)
- 失败: {failed}
- 目录已翻译: {"是" if progress.toc_translated else "否"}
- 图片翻译: {sum(progress.images_translated.values())}/{len(progress.images_translated)}
"""
    logger.console(f"翻译进度: {completed}/{total}")
    logger.tool_call("get_translation_progress", f"已完成 {completed}/{total}")
    return status


@epub_toolset.tool
def translate_toc(ctx: RunContext[EpubContext]) -> str:
    """
    翻译目录 (Table of Contents)

    返回需要翻译的目录项列表
    """
    logger = get_logger()
    logger.console("正在翻译目录...")
    book = ctx.deps.book

    if not book.toc:
        logger.tool_call("translate_toc", "此书没有目录")
        return "此书没有目录"

    toc_titles = collect_toc_titles(book.toc)
    logger.tool_call("translate_toc", f"发放 {len(toc_titles)} 条目录项")

    return (
        "目录项（共 "
        + str(len(toc_titles))
        + " 条，请按相同顺序、相同数量返回译文）:\n"
        + "\n".join(f"{i}. {t}" for i, t in enumerate(toc_titles, 1))
    )


@epub_toolset.tool
def save_translated_toc(
    ctx: RunContext[EpubContext], translated_titles: List[str]
) -> str:
    """
    保存翻译后的目录

    Args:
        translated_titles: 翻译后的目录标题列表（按顺序）

    Returns:
        保存结果
    """
    logger = get_logger()
    logger.console("正在保存目录...")
    book = ctx.deps.book

    if not book.toc:
        logger.tool_call("save_translated_toc", "此书没有目录")
        return "此书没有目录，无需保存"

    original_titles = collect_toc_titles(book.toc)
    if len(translated_titles) != len(original_titles):
        # 数量对不上会导致标题整体错位，宁可让模型重来
        logger.rejection(
            0,
            f"目录条目数不符：收到 {len(translated_titles)}，应为 {len(original_titles)}",
        )
        return (
            f"错误：目录共 {len(original_titles)} 条，但收到 {len(translated_titles)} 条译文。"
            f"请按完全相同的顺序和数量重新提交。"
        )

    book.toc = apply_toc_titles(book.toc, iter(translated_titles))

    # 侧边栏目录来自导航文档，ebooklib 不会用 book.toc 重建它，必须手动同步
    nav_updated = sync_nav_documents(ctx.deps, original_titles, translated_titles)

    # 更新进度（如果启用缓存）
    if ctx.deps.cache_manager and ctx.deps.cache_key:

        def _mark_toc(progress: TranslationProgress) -> None:
            progress.toc_translated = True
            # 缓存译文本身：续译时 book.toc 会重新从原书读出，
            # 只记一个布尔量的话，跳过翻译就等于退回原文
            progress.toc_titles = list(translated_titles)

        ctx.deps.cache_manager.update_progress(ctx.deps.cache_key, _mark_toc)

    logger.tool_call(
        "save_translated_toc",
        f"{len(translated_titles)} 条目录译文已写回，侧边栏同步 {nav_updated} 条",
    )
    if nav_updated:
        return f"✓ 目录已更新（侧边栏导航同步 {nav_updated} 条）"
    return "✓ 目录已更新"


def sync_nav_documents(
    ctx: EpubContext, original_titles: List[str], translated_titles: List[str]
) -> int:
    """把已保存的目录译文同步进 EPUB3 导航文档（侧边栏目录）。

    nav.xhtml 不在 book.toc 体系里（ebooklib 写盘时原样回写），
    所以目录译文要另写回导航文档的 <a> 文本。按标题文本做映射：
    book.toc 与 nav 的条目一一对应（同为 EPUB 的 toc 语义），
    标题文本是两者的公共键。返回替换的条目数。
    """
    mapping = dict(zip(original_titles, translated_titles))
    logger = get_logger()
    total = 0
    for nav_item in EpubTools.find_nav_documents(ctx.book):
        try:
            content = nav_item.get_content().decode("utf-8", errors="ignore")  # type: ignore
        except Exception:
            logger.error(f"导航文档解码失败: {nav_item.get_name()}")
            continue
        new_content, replaced = EpubTools.apply_nav_labels(content, mapping)
        if replaced:
            nav_item.set_content(new_content.encode("utf-8"))
            logger.info(f"导航文档 {nav_item.get_name()} 同步 {replaced} 条目录译文")
        total += replaced
    return total


@epub_toolset.tool
def list_images(ctx: RunContext[EpubContext]) -> str:
    """
    列出所有图片

    返回图片列表和翻译状态
    """
    logger = get_logger()
    images = ctx.deps.images

    # 若设置不翻译图片则直接返回无图片
    if not images or not TRANSLATE_IMAGES:
        logger.tool_call(
            "list_images",
            "此书没有图片"
            if not images
            else "图片翻译未开启（TRANSLATE_IMAGES=False）",
        )
        return "此书没有图片"

    # 获取图片翻译状态（如果启用缓存）
    images_translated = {}
    if ctx.deps.cache_manager and ctx.deps.cache_key:
        progress = ctx.deps.cache_manager.load_progress(ctx.deps.cache_key)
        if progress:
            images_translated = progress.images_translated

    image_list = []
    for idx, img in enumerate(images, 1):
        img_name = img.get_name()
        status = "✓ 已翻译" if images_translated.get(img_name) else "待翻译"
        size = len(img.get_content())
        image_list.append(f"{idx}. {img_name} ({size} bytes) - {status}")

    logger.tool_call("list_images", f"{len(images)} 张图片")
    return "图片列表:\n" + "\n".join(image_list)


@epub_toolset.tool
def get_image_base64(ctx: RunContext[EpubContext], image_index: int) -> str:
    """
    获取指定图片的 base64 编码

    Args:
        image_index: 图片索引（从 1 开始）

    Returns:
        图片的 base64 字符串
    """
    logger = get_logger()
    if not TRANSLATE_IMAGES:
        logger.tool_call("get_image_base64", "图片翻译未开启")
        return "此书没有图片"

    images = ctx.deps.images

    if image_index < 1 or image_index > len(images):
        logger.tool_error(
            "get_image_base64", f"图片索引 {image_index} 超出范围（1-{len(images)}）"
        )
        return f"错误：图片索引 {image_index} 超出范围（1-{len(images)}）"

    img = images[image_index - 1]
    img_data = img.get_content()
    base64_str = base64.b64encode(img_data).decode()

    logger.tool_call("get_image_base64", f"图片 {image_index}: {len(img_data)} bytes")
    return f"data:image/png;base64,{base64_str}"


@epub_toolset.tool
def save_translated_image(
    ctx: RunContext[EpubContext], image_index: int, image_base64: str
) -> str:
    """
    保存翻译后的图片

    Args:
        image_index: 图片索引（从 1 开始）
        image_base64: base64 编码的图片数据

    Returns:
        保存结果
    """
    logger = get_logger()
    if not TRANSLATE_IMAGES:
        logger.tool_call("save_translated_image", "图片翻译未开启")
        return "此书没有图片"

    images = ctx.deps.images

    if image_index < 1 or image_index > len(images):
        logger.tool_error(
            "save_translated_image",
            f"图片索引 {image_index} 超出范围（1-{len(images)}）",
        )
        return f"错误：图片索引 {image_index} 超出范围"

    img = images[image_index - 1]
    img_name = img.get_name()

    # 解码 base64
    if image_base64.startswith("data:"):
        # 移除 data URL 前缀
        image_base64 = image_base64.split(",", 1)[1]

    img_data = base64.b64decode(image_base64)

    # 更新图片内容
    img.set_content(img_data)

    # 保存到缓存（如果启用）
    if ctx.deps.cache_manager and ctx.deps.cache_key:
        ctx.deps.cache_manager.save_image(ctx.deps.cache_key, img_name, img_data)

        # 更新进度
        def _mark_image(progress: TranslationProgress) -> None:
            progress.images_translated[img_name] = True

        ctx.deps.cache_manager.update_progress(ctx.deps.cache_key, _mark_image)

    logger.tool_call("save_translated_image", f"图片 {image_index}: {img_name}")
    return f"✓ 已保存图片 {image_index}: {img_name}"


def merge_glossary(ctx: EpubContext, new_terms: Dict[str, str]) -> int:
    """把术语并入上下文并落盘，返回真正新增的条目数。

    这不是 Agent 工具：块级翻译路径上没有工具可调，术语来自模型追加在块译文末尾
    的术语块（见 chunk_translator.parse_terms_block）。已存在的键不覆盖，
    先出现的译名说了算，保证全书一致。
    落盘走 update_progress，读—改—写留在锁内（红线 4）。
    """
    added = {k: v for k, v in new_terms.items() if k and v and k not in ctx.glossary}
    if not added:
        return 0

    ctx.glossary.update(added)
    if ctx.cache_manager and ctx.cache_key:
        ctx.cache_manager.update_progress(
            ctx.cache_key, lambda progress: progress.glossary.update(added)
        )
    return len(added)


def finalize_chapter(ctx: EpubContext, chapter_index: int) -> tuple[bool, str]:
    """拼接全章译文、判定完整性并写入 book / 缓存。

    这不是 Agent 工具：块级路径上模型只负责"给一块原文、还一块译文"，
    保存时机由 Python 决定。判定语义与旧的 save_translated_chapter 逐条一致
    （块级标签比例判漏译、不完整也写进 book、不完整不进 completed_chapters、
    save_chapter 日志字段同名同口径，便于与历史日志对比）。

    Returns:
        (是否完整, 说明)
    """
    logger = get_logger()
    total = ctx.chunk_count(chapter_index)
    if total == 0:
        return False, f"章节 {chapter_index} 没有分块内容"

    pending = ctx.pending_chunks(chapter_index)
    if pending:
        # 有块没译文就不许保存：放行等于把漏译静默写进成品。
        logger.rejection(chapter_index, f"还有 {len(pending)} 块没有译文: {pending}")
        return False, f"还有 {len(pending)}/{total} 块没有译文"

    chapter = ctx.chapters[chapter_index - 1]
    chapter_id = chapter.get_id()
    if not chapter_id:
        logger.error(f"章节 {chapter_index} 的 id 为空，无法保存")
        return False, "章节 id 为空"

    translated_html = ctx.assembled_translation(chapter_index)

    # 全章复查：逐块校验已经在块级校验器里做过，这里兜住"每块都略微偏少、
    # 累积起来缺一大截"的情况。判定只用块级标签——全标签口径会把"内联标签被吞"
    # 误判成"整段没译"（实测第 5 章正文一字没漏，只丢 5 个 <a> 和 2 个 <em>，
    # 全标签 49/63=77.8% 就被判漏译，随后卡死在工具互相打脸的死循环里）。
    source_tags = ctx.source_tag_count(chapter_index)
    actual_tags = _count_tags(translated_html)
    source_block = ctx.source_tag_count(chapter_index, block_only=True)
    actual_block = _count_tags(translated_html, block_only=True)
    tags_missing = (
        source_block > 0 and actual_block < source_block * MIN_BLOCK_TAG_RATIO
    )
    source_inline = source_tags - source_block
    actual_inline = actual_tags - actual_block
    thin = ctx.thin_chunks(chapter_index)

    logger.console(f"正在保存章节[{chapter_index}]...")
    logger.json_line(
        {
            "event": "save_chapter",
            "chapter": chapter_index,
            "chars": len(translated_html),
            # tags / source_tags 保持全标签口径不变，便于与历史日志对比
            "tags": actual_tags,
            "source_tags": source_tags,
            "block_tags": actual_block,
            "source_block_tags": source_block,
            "inline_tags": actual_inline,
            "source_inline_tags": source_inline,
            "tags_missing": tags_missing,
            "chunks": total,
            "thin_chunks": thin,
            "chunk_attempts": ctx.chunk_attempts.get(chapter_index, {}),
        }
    )
    if thin:
        logger.console(
            f"⚠️  章节 {chapter_index} 有 {len(thin)} 块块级标签偏少：块号 {thin}",
            ConsoleLevel.VERBOSE,
        )
    if source_inline and actual_inline < source_inline * MIN_INLINE_TAG_RATIO:
        # 内联标签不足不阻塞保存，但要留痕：脚注链接、强调这类排版细节确实丢了
        logger.console(
            f"⚠️  章节 {chapter_index} 内联标签 {actual_inline}/{source_inline}"
            f"，正文完整，不影响保存",
            ConsoleLevel.VERBOSE,
        )
    if tags_missing:
        logger.incomplete(
            chapter_index, f"全章块级标签 {actual_block}/{source_block}，判定漏译"
        )
        logger.dump_buffer(chapter_index, translated_html)

    # 更新章节内容：用原章节的 soup 做模板，只替换 body 内容。
    # 注意 ebooklib 写盘时（EpubHtml.get_content）会用自家模板重建整个文档，
    # head 里只输出 item 上注册过的 links——所以必须先把【原始字节】里
    # 声明的样式表 add_link 回去，否则 CSS 链接依旧会丢（soup 模板救不了它）。
    raw_html = chapter.content.decode("utf-8", errors="ignore")
    raw_soup = BeautifulSoup(raw_html, "html.parser")
    for lnk in raw_soup.find_all("link", rel="stylesheet"):
        href = lnk.get("href")
        if not href:
            continue
        already = any(existing.get("href") == href for existing in chapter.links)
        if not already:
            chapter.add_link(href=href, rel="stylesheet", type="text/css")
            logger.console(
                f"章节 {chapter_index} 注册样式表 {href}",
                ConsoleLevel.VERBOSE,
            )

    original_html = chapter.get_content().decode("utf-8", errors="ignore")
    soup = BeautifulSoup(original_html, "html.parser")
    body = soup.find("body")
    if body:
        # 清空原 body，注入译文
        body.clear()
        # 译文可能是多个根节点，用 BeautifulSoup 解析后再添加
        translated_soup = BeautifulSoup(translated_html, "html.parser")
        for child in translated_soup.contents:
            body.append(child)
        final_html = str(soup)
    else:
        # 兜底：找不到 body 就直接用译文（极少见）
        final_html = translated_html
    chapter.set_content(final_html.encode("utf-8"))

    if ctx.cache_manager and ctx.cache_key:
        ctx.cache_manager.save_chapter(ctx.cache_key, chapter_id, final_html)

    if tags_missing:
        reason = f"全章块级标签 {actual_block}/{source_block}"
        ctx.incomplete_chapters[chapter_index] = reason
        return False, reason

    ctx.saved_chapters.add(chapter_index)
    ctx.incomplete_chapters.pop(chapter_index, None)

    # 只有完整保存才写进已完成列表，否则 --resume 会永远跳过残缺章节
    if ctx.cache_manager and ctx.cache_key:

        def _mark_done(progress: TranslationProgress) -> None:
            if chapter_id not in progress.completed_chapters:
                progress.completed_chapters.append(chapter_id)

        ctx.cache_manager.update_progress(ctx.cache_key, _mark_done)

    return True, f"块级标签 {actual_block}/{source_block}"


def finalize_epub(ctx: EpubContext, output_path: str) -> str:
    """
    完成翻译，保存 EPUB 文件

    这不是 Agent 工具：写盘时机由 Python 在校验完成度后决定，
    避免模型中途或漏章时提前落盘。

    Args:
        ctx: EPUB 翻译上下文
        output_path: 输出文件路径

    Returns:
        保存结果
    """
    get_logger().console("正在保存文件...")
    # 设置语言
    EpubTools.set_language(ctx.book, ctx.target_language)

    # 保存文件
    epub.write_epub(output_path, ctx.book)

    return f"✓ EPUB 文件已保存: {output_path}"
