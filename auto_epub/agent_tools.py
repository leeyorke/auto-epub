"""
EPUB 翻译工具集 - 使用 Toolsets 方式
"""

import base64
import re
from typing import Dict, List, Optional

from ebooklib import epub
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from .cache_manager import CacheManager
from .epub_tools import EpubTools
from .logger import ConsoleLevel, get_logger
from .settings import MIN_TAG_RATIO, TRANSLATE_IMAGES

_TAG_RE = re.compile(r"<[a-zA-Z/][^>]*>")


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


def _count_tags(html: str) -> int:
    """统计 HTML 标签数量。

    译文要求原样保留标签，因此标签数是比字符数更可靠的完整性信号：
    中文译文字符数天然比英文原文少一半左右，用字符数判断会大量误报。
    """
    return len(_TAG_RE.findall(html))


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
        # 翻译内容缓冲区：模型可以分多次写入，避免单次工具调用参数过大
        self.translation_buffer: Dict[int, str] = {}
        # 待翻译分块队列，按章节索引隔离，避免多个章节的分块互相串位
        self.untranslated_buffer: Dict[int, List[str]] = {}
        # 各章节原文的标签数，用于保存时校验译文是否严重缺失
        self.source_tags: Dict[int, int] = {}
        # 各章节 save 被拒次数，避免完整性校验把模型卡死在重试循环里
        self.save_rejections: Dict[int, int] = {}

    def prepare_chapter(self, chapter_index: int) -> int:
        """切分章节内容并重置该章状态，返回分块数。

        切分由 Python 在 run 之前完成，不作为 Agent 工具暴露：
        模型重复调用切分会清空已攒的译文，导致永远保存不了。

        Returns:
            分块数量；章节内容为空或解码失败时返回 0
        """
        self.translation_buffer.pop(chapter_index, None)
        self.save_rejections.pop(chapter_index, None)
        self.untranslated_buffer[chapter_index] = []
        self.source_tags[chapter_index] = 0

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
        self.untranslated_buffer[chapter_index] = list(chunks)
        self.source_tags[chapter_index] = sum(_count_tags(c) for c in chunks)

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
        """清理某章节的所有中间状态（重试该章前调用）"""
        self.untranslated_buffer.pop(chapter_index, None)
        self.translation_buffer.pop(chapter_index, None)
        self.source_tags.pop(chapter_index, None)
        self.save_rejections.pop(chapter_index, None)


# 创建工具集
epub_toolset: FunctionToolset[EpubContext] = FunctionToolset()


@epub_toolset.tool
def get_book_info(ctx: RunContext[EpubContext]) -> str:
    """
    获取 EPUB 书籍基本信息

    返回书籍的标题、作者、语言、章节数等信息
    """
    get_logger().console("正在获取书籍信息...")
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
    get_logger().console("正在查看章节信息...")
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
def is_untranslated_buffer_empty(
    ctx: RunContext[EpubContext], chapter_index: int
) -> str:
    """
    检查指定章节的待翻译分块是否已全部取完

    Args:
        chapter_index: 章节索引（从 1 开始）

    Returns:
        剩余分块情况的描述
    """
    remaining = len(ctx.deps.untranslated_buffer.get(chapter_index, []))
    if remaining == 0:
        get_logger().console(f"章节{chapter_index}的所有内容片段已全部翻译完成")
        return f"✓ 章节 {chapter_index} 的分块已全部取完，可以调用 save_translated_chapter 保存。"
    return (
        f"章节 {chapter_index} 还剩 {remaining} 个分块未翻译，"
        f"请继续调用 get_untranslated_content。"
    )


@epub_toolset.tool
def get_untranslated_content(ctx: RunContext[EpubContext], chapter_index: int) -> str:
    """
    从指定章节的待翻译队列中取出下一个 HTML 分块

    每次调用取出一块，需重复调用直到取完该章节的所有分块。
    取出的内容由你自己翻译，然后通过 store_translation_chunk 写回。

    Args:
        chapter_index: 章节索引（从 1 开始）

    Returns:
        待翻译的 HTML 内容块
    """
    if chapter_index not in ctx.deps.untranslated_buffer:
        return f"错误：章节 {chapter_index} 不在本次任务范围内"

    pending = ctx.deps.untranslated_buffer[chapter_index]
    if not pending:
        get_logger().console(f"章节{chapter_index}的所有内容片段已全部翻译完成")
        return (
            f"章节 {chapter_index} 的分块已全部取完。"
            f"请确认所有译文都已通过 store_translation_chunk 写入，然后保存章节。"
        )

    chunk = pending.pop(0)
    remaining = len(pending)
    get_logger().console(f"正在翻译章节{chapter_index}...（剩余 {remaining} 块）")
    get_logger().info(
        f"章节 {chapter_index} 发放分块: tokens={EpubTools.count_tokens(chunk)} "
        f"chars={len(chunk)} tags={_count_tags(chunk)}，剩余 {remaining} 块"
    )

    hint = (
        "取完了，翻译并存入后即可保存本章。"
        if remaining == 0
        else f"本章还剩 {remaining} 块，存入本块译文后请继续调用本工具。"
    )
    # 提示放在正文之后，避免模型误把它当作待翻译内容的一部分抄进译文
    return f"{chunk}\n\n---\n[系统提示] {hint}"


@epub_toolset.tool
def store_translation_chunk(
    ctx: RunContext[EpubContext], chapter_index: int, translated_html: str
) -> str:
    """
    存储章节翻译内容片段

    用于分块保存大章节的翻译内容。可以多次调用此工具写入同一章节的不同片段，
    全部写入完成后调用 save_translated_chapter 一次性保存。

    Args:
        chapter_index: 章节索引（从 1 开始）
        translated_html: 翻译后的 HTML 内容片段

    Returns:
        存储结果消息
    """
    if chapter_index < 1 or chapter_index > len(ctx.deps.chapters):
        return f"错误：章节索引 {chapter_index} 超出范围"

    if not translated_html.strip():
        return "错误：传入的译文为空"

    prev = ctx.deps.translation_buffer.get(chapter_index, "")
    ctx.deps.translation_buffer[chapter_index] = prev + translated_html

    total = len(ctx.deps.translation_buffer[chapter_index])
    remaining = len(ctx.deps.untranslated_buffer.get(chapter_index, []))
    get_logger().info(
        f"章节 {chapter_index} 写入译文: chars={len(translated_html)} "
        f"tags={_count_tags(translated_html)}，累计 {total} 字符，剩余 {remaining} 块"
    )
    if remaining > 0:
        return (
            f"✓ 已存储章节 {chapter_index} 的翻译片段（累计 {total} 字符）。"
            f"本章还剩 {remaining} 个分块未取，请继续调用 get_untranslated_content。"
        )
    return (
        f"✓ 已存储章节 {chapter_index} 的翻译片段（累计 {total} 字符）。"
        f"本章分块已全部取完，确认译文完整后可调用 save_translated_chapter 保存。"
    )


@epub_toolset.tool
def save_translated_chapter(ctx: RunContext[EpubContext], chapter_index: int) -> str:
    """
    保存翻译后的章节

    从翻译缓冲区中取出之前通过 store_translation_chunk 存入的内容，保存到 EPUB。
    调用前必须确保该章节的所有分块都已取出并翻译写入。

    Args:
        chapter_index: 章节索引（从 1 开始）

    Returns:
        保存结果消息
    """
    if chapter_index < 1 or chapter_index > len(ctx.deps.chapters):
        return f"错误：章节索引 {chapter_index} 超出范围"

    # 护栏 1：还有分块没取出来翻译，说明会漏内容
    remaining = len(ctx.deps.untranslated_buffer.get(chapter_index, []))
    if remaining > 0:
        get_logger().rejection(chapter_index, f"还有 {remaining} 块未翻译")
        return (
            f"错误：章节 {chapter_index} 还有 {remaining} 个分块未翻译，不能保存。"
            f"请继续调用 get_untranslated_content 取出剩余分块翻译。"
        )

    chapter = ctx.deps.chapters[chapter_index - 1]
    chapter_id = chapter.get_id()

    if not chapter_id:
        return "错误：章节id获取为空"

    translated_html = ctx.deps.translation_buffer.get(chapter_index, "")
    if not translated_html:
        get_logger().rejection(chapter_index, "翻译缓冲区为空")
        return (
            f"错误：章节 {chapter_index} 的翻译缓冲区为空，"
            f"请先调用 store_translation_chunk 写入译文再保存。"
        )

    # 护栏 2：译文标签数远少于原文，说明有整段内容被省略。
    # 用标签数而非字符数：中文译文字符数天然比英文少一半左右，按字符判断会误报。
    # 首次拒绝并要求补齐，第二次仍不达标则放行并告警，避免卡死在重试循环。
    source_tags = ctx.deps.source_tags.get(chapter_index, 0)
    actual_tags = _count_tags(translated_html)
    tags_missing = source_tags > 0 and actual_tags < source_tags * MIN_TAG_RATIO
    rejections = ctx.deps.save_rejections.get(chapter_index, 0)

    if tags_missing and rejections < 1:
        ctx.deps.save_rejections[chapter_index] = rejections + 1
        logger = get_logger()
        logger.rejection(chapter_index, f"标签数 {actual_tags}/{source_tags}，疑似漏译")
        logger.dump_buffer(chapter_index, translated_html)
        return (
            f"错误：章节 {chapter_index} 的译文只有 {actual_tags} 个 HTML 标签，"
            f"而原文有 {source_tags} 个，说明有内容被省略了。"
            f"注意：不要重新获取原文，已取出的分块不会再发放。"
            f"请把之前遗漏未译的那部分内容补译，"
            f"用 store_translation_chunk 追加写入（会自动拼接到已有译文后面），"
            f"然后再次保存。"
        )

    get_logger().console(f"正在保存章节[{chapter_index}]...")
    get_logger().json_line(
        {
            "event": "save_chapter",
            "chapter": chapter_index,
            "chars": len(translated_html),
            "tags": actual_tags,
            "source_tags": source_tags,
            "tags_missing": tags_missing,
            "rejections": rejections,
        }
    )
    if tags_missing:
        get_logger().console(
            f"⚠️  章节 {chapter_index} 译文可能不完整："
            f"标签数 {actual_tags}/{source_tags}",
            ConsoleLevel.VERBOSE,
        )

    # 确认无误后才真正消费缓冲区
    ctx.deps.translation_buffer.pop(chapter_index, None)
    ctx.deps.untranslated_buffer.pop(chapter_index, None)

    # 更新章节内容
    chapter.set_content(translated_html.encode("utf-8"))

    # 保存到缓存（如果启用）
    if ctx.deps.cache_manager and ctx.deps.cache_key:
        ctx.deps.cache_manager.save_chapter(
            ctx.deps.cache_key, chapter_id, translated_html
        )

        # 更新进度
        progress = ctx.deps.cache_manager.load_progress(ctx.deps.cache_key)
        if progress and chapter_id not in progress.completed_chapters:
            progress.completed_chapters.append(chapter_id)
            ctx.deps.cache_manager.save_progress(ctx.deps.cache_key, progress)

    return f"✓ 已保存章节 {chapter_index}: {chapter.get_name()}"


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

    # 保存到缓存（如果启用）
    if ctx.deps.cache_manager and ctx.deps.cache_key:
        progress = ctx.deps.cache_manager.load_progress(ctx.deps.cache_key)
        if progress:
            progress.glossary.update(new_terms)
            ctx.deps.cache_manager.save_progress(ctx.deps.cache_key, progress)

    return f"✓ 已更新 {len(new_terms)} 个术语"


@epub_toolset.tool
def get_glossary(ctx: RunContext[EpubContext]) -> str:
    """
    获取当前的术语表

    返回已记录的所有专有名词翻译对照
    """
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
    if not ctx.deps.cache_manager or not ctx.deps.cache_key:
        return "缓存未启用，无法获取进度"

    progress = ctx.deps.cache_manager.load_progress(ctx.deps.cache_key)

    if not progress:
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
    get_logger().console(f"翻译进度: {completed}/{total}")
    return status


@epub_toolset.tool
def translate_toc(ctx: RunContext[EpubContext]) -> str:
    """
    翻译目录 (Table of Contents)

    返回需要翻译的目录项列表
    """
    get_logger().console("正在翻译目录...")
    book = ctx.deps.book

    if not book.toc:
        return "此书没有目录"

    toc_titles = collect_toc_titles(book.toc)

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
    get_logger().console("正在保存目录...")
    book = ctx.deps.book

    if not book.toc:
        return "此书没有目录，无需保存"

    original_titles = collect_toc_titles(book.toc)
    if len(translated_titles) != len(original_titles):
        # 数量对不上会导致标题整体错位，宁可让模型重来
        get_logger().rejection(
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
        progress = ctx.deps.cache_manager.load_progress(ctx.deps.cache_key)
        if progress:
            progress.toc_translated = True
            # 缓存译文本身：续译时 book.toc 会重新从原书读出，
            # 只记一个布尔量的话，跳过翻译就等于退回原文
            progress.toc_titles = list(translated_titles)
            ctx.deps.cache_manager.save_progress(ctx.deps.cache_key, progress)

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
    images = ctx.deps.images

    # 若设置不翻译图片则直接返回无图片
    if not images or not TRANSLATE_IMAGES:
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
    if not TRANSLATE_IMAGES:
        return "此书没有图片"

    images = ctx.deps.images

    if image_index < 1 or image_index > len(images):
        return f"错误：图片索引 {image_index} 超出范围（1-{len(images)}）"

    img = images[image_index - 1]
    img_data = img.get_content()
    base64_str = base64.b64encode(img_data).decode()

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
    if not TRANSLATE_IMAGES:
        return "此书没有图片"

    images = ctx.deps.images

    if image_index < 1 or image_index > len(images):
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
        progress = ctx.deps.cache_manager.load_progress(ctx.deps.cache_key)
        if progress:
            progress.images_translated[img_name] = True
            ctx.deps.cache_manager.save_progress(ctx.deps.cache_key, progress)

    return f"✓ 已保存图片 {image_index}: {img_name}"


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
