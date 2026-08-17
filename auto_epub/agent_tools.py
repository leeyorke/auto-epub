"""
EPUB 翻译工具集 - 使用 Toolsets 方式
"""

import base64
import re
from typing import Dict, List, Optional, Set

from ebooklib import epub
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from .cache_manager import CacheManager
from .epub_tools import EpubTools
from .logger import ConsoleLevel, get_logger
from .models import TranslationProgress
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


def _out_of_scope(tool: str, chapter_index: int) -> str:
    """本次 run 没准备过这一章时的统一错误返回

    多半是模型把 chapter_index 写错了（本次任务只准备了一章）。四个章节级
    工具都会走到这里，日志统一在这里打，漏一个就可能出现"整段空转日志全空"。
    """
    reason = f"章节 {chapter_index} 不在本次任务范围内"
    get_logger().tool_error(tool, reason)
    return f"错误：{reason}"


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
        # 曾经用"取出即弹出"的队列，一旦某块的 store 调用被截断，这块原文就
        # 永久消失了：模型只能跳过它继续下一块（静默漏译），或者整章重来。
        # 改成按块号寻址后，未写入译文的块会被反复发放，直到真的存进来。
        self.chapter_chunks: Dict[int, List[str]] = {}
        # 逐块译文 {章节索引: {块号(从 0 起): 译文}}。块号由模型回传，
        # 缺号即未完成；拼接时按块号排序，不依赖模型的写入顺序。
        self.chunk_translations: Dict[int, Dict[int, str]] = {}
        # 已就"疑似漏译"提醒过的块 {章节索引: {块号}}，
        # 同一块只提醒一次，避免模型卡在补译-拒绝循环里
        self.chunk_warned: Dict[int, Set[int]] = {}
        # 保存成功（且完整）的章节索引，未启用缓存时用它判断落盘情况
        self.saved_chapters: Set[int] = set()
        # 保存了但判定不完整的章节 {章节索引: 原因}，供编排层如实报告
        self.incomplete_chapters: Dict[int, str] = {}

    # ---------- 分块状态查询（供工具和编排层共用） ----------

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

    def source_tag_count(self, chapter_index: int) -> int:
        """全章原文的标签数（由分块实时统计，不额外维护一份状态）"""
        return sum(_count_tags(c) for c in self.chapter_chunks.get(chapter_index, []))

    def thin_chunks(self, chapter_index: int) -> List[int]:
        """返回译文标签数明显少于原文的块号——疑似块内漏译"""
        stored = self.chunk_translations.get(chapter_index, {})
        thin = []
        for i, source in enumerate(self.chapter_chunks.get(chapter_index, [])):
            expected = _count_tags(source)
            if expected and _count_tags(stored.get(i, "")) < expected * MIN_TAG_RATIO:
                thin.append(i)
        return thin

    def prepare_chapter(self, chapter_index: int) -> int:
        """切分章节内容并重置该章状态，返回分块数。

        切分由 Python 在 run 之前完成，不作为 Agent 工具暴露：
        模型重复调用切分会清空已攒的译文，导致永远保存不了。

        Returns:
            分块数量；章节内容为空或解码失败时返回 0
        """
        self.chapter_chunks[chapter_index] = []
        self.chunk_translations[chapter_index] = {}
        self.chunk_warned[chapter_index] = set()
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
        """清理某章节的所有中间状态（重试该章前调用）"""
        self.chapter_chunks.pop(chapter_index, None)
        self.chunk_translations.pop(chapter_index, None)
        self.chunk_warned.pop(chapter_index, None)
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
def check_chapter_progress(ctx: RunContext[EpubContext], chapter_index: int) -> str:
    """
    查看指定章节还有哪些分块没有写入译文

    Args:
        chapter_index: 章节索引（从 1 开始）

    Returns:
        剩余分块情况的描述
    """
    logger = get_logger()
    total = ctx.deps.chunk_count(chapter_index)
    if total == 0:
        return _out_of_scope("check_chapter_progress", chapter_index)

    pending = ctx.deps.pending_chunks(chapter_index)
    if not pending:
        logger.console(f"章节{chapter_index}的所有内容片段已全部翻译完成")
        logger.tool_call(
            "check_chapter_progress", f"章节 {chapter_index} 的 {total} 块均已有译文"
        )
        return (
            f"✓ 章节 {chapter_index} 的 {total} 个分块都已写入译文，"
            f"可以调用 save_translated_chapter 保存。"
        )
    logger.tool_call(
        "check_chapter_progress",
        f"章节 {chapter_index} 尚缺 {len(pending)}/{total} 块 {pending}",
    )
    return (
        f"章节 {chapter_index} 还有 {len(pending)}/{total} 个分块没有译文，"
        f"块号（chunk_index）为 {pending}，"
        f"请调用 get_untranslated_content 继续翻译。"
    )


@epub_toolset.tool
def get_untranslated_content(ctx: RunContext[EpubContext], chapter_index: int) -> str:
    """
    获取指定章节中下一个还没有译文的 HTML 分块

    每次调用返回一块，翻译后必须用 store_translation_chunk 连同块号写回，
    写回成功才算完成；否则再次调用本工具会重新拿到同一块。
    重复调用直到该章节所有分块都有译文。

    Args:
        chapter_index: 章节索引（从 1 开始）

    Returns:
        待翻译的 HTML 内容块，以及它的块号（chunk_index）
    """
    logger = get_logger()
    total = ctx.deps.chunk_count(chapter_index)
    if total == 0:
        return _out_of_scope("get_untranslated_content", chapter_index)

    pending = ctx.deps.pending_chunks(chapter_index)
    if not pending:
        logger.console(f"章节{chapter_index}的所有内容片段已全部翻译完成")
        logger.tool_call(
            "get_untranslated_content", f"章节 {chapter_index} 已无待译分块"
        )
        return (
            f"章节 {chapter_index} 的 {total} 个分块都已写入译文，"
            f"请调用 save_translated_chapter 保存本章。"
        )

    index = pending[0]
    chunk = ctx.deps.chapter_chunks[chapter_index][index]
    remaining = len(pending)
    logger.console(f"正在翻译章节{chapter_index}...（剩余 {remaining} 块）")
    logger.info(
        f"章节 {chapter_index} 发放块 {index}: tokens={EpubTools.count_tokens(chunk)} "
        f"chars={len(chunk)} tags={_count_tags(chunk)}，本章尚缺 {remaining} 块"
    )

    hint = (
        f"这是第 {index + 1}/{total} 块，chunk_index={index}。"
        f"翻译后调用 store_translation_chunk({chapter_index}, {index}, 译文) 写入。"
    )
    if remaining == 1:
        hint += "这是最后一块，写入后即可保存本章。"
    else:
        hint += f"本章还缺 {remaining} 块，写入后请继续调用本工具。"
    # 提示放在正文之后，避免模型误把它当作待翻译内容的一部分抄进译文
    return f"{chunk}\n\n---\n[系统提示] {hint}"


@epub_toolset.tool
def store_translation_chunk(
    ctx: RunContext[EpubContext],
    chapter_index: int,
    chunk_index: int,
    translated_html: str,
) -> str:
    """
    写入某个分块的译文

    chunk_index 必须是 get_untranslated_content 返回的那个块号，写错会导致
    译文错位。同一个 chunk_index 可以多次调用，内容会按调用顺序追加拼接，
    适合单块译文太长、需要分几次写入的情况。

    Args:
        chapter_index: 章节索引（从 1 开始）
        chunk_index: 块号，即 get_untranslated_content 返回的 chunk_index
        translated_html: 该块翻译后的 HTML 内容

    Returns:
        存储结果消息，包含本块的完整性检查结论
    """
    logger = get_logger()
    total = ctx.deps.chunk_count(chapter_index)
    if total == 0:
        return _out_of_scope("store_translation_chunk", chapter_index)

    if chunk_index < 0 or chunk_index >= total:
        pending = ctx.deps.pending_chunks(chapter_index)
        logger.tool_error(
            "store_translation_chunk",
            f"块号 {chunk_index} 超出范围（章节 {chapter_index} 共 {total} 块），"
            f"尚缺 {pending}",
        )
        return (
            f"错误：块号 {chunk_index} 超出范围，章节 {chapter_index} 只有 "
            f"{total} 块（合法块号 0~{total - 1}）。当前还缺的块号：{pending}。"
        )

    if not translated_html.strip():
        logger.tool_error(
            "store_translation_chunk",
            f"章节 {chapter_index} 块 {chunk_index} 传入的译文为空",
        )
        return "错误：传入的译文为空"

    stored = ctx.deps.chunk_translations.setdefault(chapter_index, {})
    appended = bool(stored.get(chunk_index, "").strip())
    stored[chunk_index] = stored.get(chunk_index, "") + translated_html

    source_chunk = ctx.deps.chapter_chunks[chapter_index][chunk_index]
    expected_tags = _count_tags(source_chunk)
    actual_tags = _count_tags(stored[chunk_index])
    pending = ctx.deps.pending_chunks(chapter_index)

    logger.info(
        f"章节 {chapter_index} 写入块 {chunk_index}: chars={len(translated_html)} "
        f"tags={actual_tags}/{expected_tags}"
        f"{'（追加）' if appended else ''}，本章尚缺 {len(pending)} 块"
    )

    # 完整性校验放在写入时刻：此时模型手里还有这块原文，能直接补译；
    # 等到 save 时才发现漏译，模型已经不知道漏的是哪一段了。
    warned = ctx.deps.chunk_warned.setdefault(chapter_index, set())
    if expected_tags and actual_tags < expected_tags * MIN_TAG_RATIO:
        if chunk_index not in warned:
            # 同一块只提醒一次，避免模型卡在补译-拒绝的死循环里
            warned.add(chunk_index)
            logger.incomplete(
                chapter_index,
                f"块 {chunk_index} 标签数 {actual_tags}/{expected_tags}，要求补译",
            )
            return (
                f"⚠️ 块 {chunk_index} 的译文已存入，但只有 {actual_tags} 个 HTML 标签，"
                f"原文有 {expected_tags} 个，说明有内容没译到。"
                f"请只把遗漏的那部分补译出来，再次调用 "
                f"store_translation_chunk({chapter_index}, {chunk_index}, 补译内容)"
                f"追加进去（会自动拼到已有译文后面），不要重复已译内容。"
            )
        logger.console(
            f"⚠️  章节 {chapter_index} 块 {chunk_index} 译文偏短："
            f"标签数 {actual_tags}/{expected_tags}，已放行",
            ConsoleLevel.VERBOSE,
        )
    elif expected_tags and actual_tags > expected_tags * 1.5:
        # 标签数明显多于原文，通常是块号写错、把别的块的译文追加到这里了
        logger.info(
            f"章节 {chapter_index} 块 {chunk_index} 标签数偏多 "
            f"{actual_tags}/{expected_tags}，注意块号是否写错"
        )

    if pending:
        return (
            f"✓ 已存入章节 {chapter_index} 的块 {chunk_index}"
            f"（标签数 {actual_tags}/{expected_tags}）。"
            f"本章还缺 {len(pending)} 块，块号 {pending}，"
            f"请继续调用 get_untranslated_content。"
        )
    return (
        f"✓ 已存入章节 {chapter_index} 的块 {chunk_index}"
        f"（标签数 {actual_tags}/{expected_tags}）。"
        f"本章 {total} 个分块都有译文了，可以调用 save_translated_chapter 保存。"
    )


@epub_toolset.tool
def save_translated_chapter(ctx: RunContext[EpubContext], chapter_index: int) -> str:
    """
    保存翻译后的章节

    把之前通过 store_translation_chunk 写入的各块译文按块号顺序拼接，存入 EPUB。
    调用前该章节的每一个分块都必须有译文。

    Args:
        chapter_index: 章节索引（从 1 开始）

    Returns:
        保存结果消息
    """
    total = ctx.deps.chunk_count(chapter_index)
    if total == 0:
        return _out_of_scope("save_translated_chapter", chapter_index)

    # 护栏：只要有块没译文就不许保存。分块可以反复获取，模型总能补上，
    # 因此这里不设放行次数——放行等于把漏译静默写进成品。
    pending = ctx.deps.pending_chunks(chapter_index)
    if pending:
        get_logger().rejection(
            chapter_index, f"还有 {len(pending)} 块没有译文: {pending}"
        )
        return (
            f"错误：章节 {chapter_index} 还有 {len(pending)} 个分块没有译文，"
            f"块号 {pending}，不能保存。"
            f"请调用 get_untranslated_content 取出剩余分块翻译。"
        )

    chapter = ctx.deps.chapters[chapter_index - 1]
    chapter_id = chapter.get_id()

    if not chapter_id:
        get_logger().tool_error(
            "save_translated_chapter", f"章节 {chapter_index} 的 id 为空"
        )
        return "错误：章节id获取为空"

    translated_html = ctx.deps.assembled_translation(chapter_index)

    # 全章标签数复查：逐块校验已经在 store 时做过，这里兜住"每块都略微偏少、
    # 累积起来缺一大截"的情况。用标签数而非字符数：中文译文字符数天然比英文
    # 少一半左右，按字符判断会大量误报。
    source_tags = ctx.deps.source_tag_count(chapter_index)
    actual_tags = _count_tags(translated_html)
    tags_missing = source_tags > 0 and actual_tags < source_tags * MIN_TAG_RATIO
    thin = ctx.deps.thin_chunks(chapter_index)

    logger = get_logger()
    logger.console(f"正在保存章节[{chapter_index}]...")
    logger.json_line(
        {
            "event": "save_chapter",
            "chapter": chapter_index,
            "chars": len(translated_html),
            "tags": actual_tags,
            "source_tags": source_tags,
            "tags_missing": tags_missing,
            "chunks": total,
            "thin_chunks": thin,
        }
    )
    if thin:
        logger.console(
            f"⚠️  章节 {chapter_index} 有 {len(thin)} 块译文偏短：块号 {thin}",
            ConsoleLevel.VERBOSE,
        )
    if tags_missing:
        logger.incomplete(
            chapter_index, f"全章标签数 {actual_tags}/{source_tags}，判定漏译"
        )
        logger.dump_buffer(chapter_index, translated_html)

    # 更新章节内容：即使判定不完整也写进去，部分译文比整章原文有用；
    # 但不标记完成，交给上层重试 / 下次 --resume 重译。
    chapter.set_content(translated_html.encode("utf-8"))

    if ctx.deps.cache_manager and ctx.deps.cache_key:
        ctx.deps.cache_manager.save_chapter(
            ctx.deps.cache_key, chapter_id, translated_html
        )

    if tags_missing:
        reason = f"全章标签数 {actual_tags}/{source_tags}"
        ctx.deps.incomplete_chapters[chapter_index] = reason
        return (
            f"章节 {chapter_index} 的译文已写入，但全章只有 {actual_tags} 个 HTML "
            f"标签，原文有 {source_tags} 个，判定为漏译，本章不算完成。"
            f"请不要再重复保存，本次任务到此结束。"
        )

    ctx.deps.saved_chapters.add(chapter_index)
    ctx.deps.incomplete_chapters.pop(chapter_index, None)

    # 只有完整保存才写进已完成列表，否则 --resume 会永远跳过残缺章节
    if ctx.deps.cache_manager and ctx.deps.cache_key:

        def _mark_done(progress: TranslationProgress) -> None:
            if chapter_id not in progress.completed_chapters:
                progress.completed_chapters.append(chapter_id)

        ctx.deps.cache_manager.update_progress(ctx.deps.cache_key, _mark_done)

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
