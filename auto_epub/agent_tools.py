"""
EPUB 翻译工具集 - 使用 Toolsets 方式
"""

import base64
from typing import Dict, List, Optional

from bs4 import BeautifulSoup
from ebooklib import epub
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from .cache_manager import CacheManager
from .epub_tools import EpubTools
from .settings import INPUT_MAX_TOKENS, TRANSLATE_IMAGES


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


# 创建工具集
epub_toolset: FunctionToolset[EpubContext] = FunctionToolset()


@epub_toolset.tool
def get_book_info(ctx: RunContext[EpubContext]) -> str:
    """
    获取 EPUB 书籍基本信息

    返回书籍的标题、作者、语言、章节数等信息
    """
    print("正在获取书籍信息...")
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
    print("正在查看章节信息...")
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
def get_chapter_content(ctx: RunContext[EpubContext], chapter_index: int) -> str:
    """
    获取指定章节的内容

    Args:
        chapter_index: 章节索引（从 1 开始）

    Returns:
        章节的 HTML 内容d
    """
    print(f"正在读取章节[{chapter_index}]内容...")
    if chapter_index < 1 or chapter_index > len(ctx.deps.chapters):
        return f"错误：章节索引 {chapter_index} 超出范围（1-{len(ctx.deps.chapters)}）"

    chapter = ctx.deps.chapters[chapter_index - 1]
    data = chapter.get_content()
    if not data:
        return ""

    try:
        content = data.decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"❌ 错误 - {type(e).__name__}")
        return ""

    return content


@epub_toolset.tool
def translate_chapter_content(ctx: RunContext[EpubContext], html_content: str) -> str:
    """
    翻译章节的 HTML 内容

    这个工具只翻译 HTML 中的文本内容，保留所有标签和结构

    Args:
        html_content: 要翻译的 HTML 内容

    Returns:
        翻译后的 HTML 内容
    """
    print("正在翻译章节...")
    # 解析 HTML
    soup = BeautifulSoup(html_content, "html.parser")
    body = soup.find("body")

    if not body:
        # 如果没有 body 标签，直接返回
        return html_content

    # 提取 body 内容
    body_content = "".join(str(child) for child in body.children)

    # 检查是否需要分块
    token_count = EpubTools.count_tokens(body_content)

    if token_count > INPUT_MAX_TOKENS:
        # 分块翻译
        chunks = EpubTools.split_html_content(body_content)
        translated_chunks = []

        for chunk in chunks:
            # TODO: 明天需要检查这里的代码，需要测试下效果，看是否真的根据分块
            # TODO: 提示去翻译了，如果没有分块翻译，那么需要在提示词中提示一下
            # 这里实际上应该调用 LLM，但在 tool 中我们返回指示
            # Agent 会接收到这个内容，然后自己翻译
            translated_chunks.append(f"[需要翻译的内容块]\n{chunk}")

        return "\n\n[分块翻译]\n\n".join(translated_chunks)

    # 返回需要翻译的内容
    # 实际的翻译由 Agent 自己完成
    return body_content


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

    prev = ctx.deps.translation_buffer.get(chapter_index, "")
    ctx.deps.translation_buffer[chapter_index] = prev + translated_html

    total = len(ctx.deps.translation_buffer[chapter_index])
    return f"✓ 已存储章节 {chapter_index} 的翻译片段（累计 {total} 字符）"


@epub_toolset.tool
def save_translated_chapter(ctx: RunContext[EpubContext], chapter_index: int) -> str:
    """
    保存翻译后的章节

    从翻译缓冲区中取出之前通过 store_translation_chunk 存入的内容，保存到 EPUB。
    调用前需确保已通过 store_translation_chunk 写入翻译内容。

    Args:
        chapter_index: 章节索引（从 1 开始）

    Returns:
        保存结果消息
    """
    print(f"正在保存章节[{chapter_index}]...")
    if chapter_index < 1 or chapter_index > len(ctx.deps.chapters):
        return f"错误：章节索引 {chapter_index} 超出范围"

    chapter = ctx.deps.chapters[chapter_index - 1]
    chapter_id = chapter.get_id()

    if not chapter_id:
        return "错误：章节id获取为空"

    # 从缓冲区取翻译内容
    translated_html = ctx.deps.translation_buffer.pop(chapter_index, "")
    if not translated_html:
        return "错误：未找到翻译内容，请先调用 store_translation_chunk 写入翻译"

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
    print(f"翻译进度: {completed}/{total}")
    return status


@epub_toolset.tool
def translate_toc(ctx: RunContext[EpubContext]) -> str:
    """
    翻译目录 (Table of Contents)

    返回需要翻译的目录项列表
    """
    print("正在翻译目录...")
    book = ctx.deps.book

    if not book.toc:
        return "此书没有目录"

    def extract_toc_titles(toc_items, level=0):
        """递归提取目录标题"""
        titles = []
        for item in toc_items:
            if isinstance(item, epub.Link):
                titles.append(f"{'  ' * level}- {item.title}")
            elif isinstance(item, tuple):
                section, children = item
                if isinstance(section, epub.Link):
                    titles.append(f"{'  ' * level}- {section.title}")
                titles.extend(extract_toc_titles(children, level + 1))
        return titles

    toc_titles = extract_toc_titles(book.toc)

    return "目录项:\n" + "\n".join(toc_titles)


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
    print("正在保存目录...")
    book = ctx.deps.book

    if not book.toc:
        return "此书没有目录，无需保存"

    # 这里需要重建目录结构
    # 为简化，我们假设标题数量匹配
    def update_toc_titles(toc_items, titles_iter):
        """递归更新目录标题"""
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
                updated_children = update_toc_titles(children, titles_iter)
                updated.append((new_section, updated_children))
        return updated

    titles_iter = iter(translated_titles)
    book.toc = update_toc_titles(book.toc, titles_iter)

    # 更新进度（如果启用缓存）
    if ctx.deps.cache_manager and ctx.deps.cache_key:
        progress = ctx.deps.cache_manager.load_progress(ctx.deps.cache_key)
        if progress:
            progress.toc_translated = True
            ctx.deps.cache_manager.save_progress(ctx.deps.cache_key, progress)

    return "✓ 目录已更新"


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


@epub_toolset.tool
def finalize_epub(ctx: RunContext[EpubContext], output_path: str) -> str:
    """
    完成翻译，保存 EPUB 文件

    Args:
        output_path: 输出文件路径

    Returns:
        保存结果
    """
    print("正在保存文件...")
    # 设置语言
    EpubTools.set_language(ctx.deps.book, ctx.deps.target_language)

    # 保存文件
    epub.write_epub(output_path, ctx.deps.book)

    return f"✓ EPUB 文件已保存: {output_path}"
