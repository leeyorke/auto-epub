"""
EPUB 翻译器 - 使用 Agent + Toolsets 方式
"""

from pathlib import Path
from typing import Optional, Union

from ebooklib import epub
from pydantic_ai import Agent, UsageLimits

from .agent_tools import (
    EpubContext,
    apply_toc_titles,
    collect_toc_titles,
    finalize_epub,
    sync_nav_documents,
)
from .cache_manager import CacheManager
from .epub_tools import EpubTools
from .logger import ConsoleLevel, get_logger, init_logger
from .models import TranslationProgress
from .settings import MAX_CHAPTER_RETRIES, MAX_REQUESTS

# 模型偶尔会把工具调用写成纯文本而不走 function calling 通道，
# 这种回复会被当作最终输出导致 run 提前结束，需要识别出来判定为失败。
_LEAKED_TOOL_CALL_MARKERS = ("<tool_call", "<function=", "</function>")


class EpubTranslator:
    """EPUB 翻译器 - 使用 Agent 智能调度工具"""

    def __init__(self, agent: Agent[EpubContext, str], cache_enabled: bool = True):
        """
        Args:
            agent: 配置好的 pydantic_ai Agent（包含 toolsets）
            cache_enabled: 是否启用缓存
        """
        self.agent = agent
        self.cache_manager = CacheManager() if cache_enabled else None

    async def translate_epub(
        self,
        input_file: str,
        target_language: str,
        translate_images: bool = False,
        translate_toc: bool = True,
        resume: bool = True,
        console_level: Optional[Union[int, ConsoleLevel]] = None,
    ) -> str:
        """
        翻译整个 EPUB 文件

        Args:
            input_file: 输入文件路径
            target_language: 目标语言代码
            translate_images: 是否翻译图片
            translate_toc: 是否翻译目录
            resume: 是否支持断点续传
            console_level: 控制台详细程度（None 表示沿用模块级默认）

        Returns:
            输出文件路径
        """
        # 诊断日志：文件名做日志名，一次运行一个文件；
        # 控制台详细程度由 console_level 注入，文件日志始终完整
        self.logger = init_logger(Path(input_file).stem, console_level=console_level)

        self.logger.console(f"\n📚 开始翻译 EPUB: {input_file}")
        self.logger.console(f"🎯 目标语言: {target_language}\n")
        self.logger.info(f"输入文件: {input_file}，目标语言: {target_language}")
        if self.logger.log_file:
            self.logger.console(f"📝 诊断日志: {self.logger.log_file}\n")

        # 1. 加载 EPUB
        book = epub.read_epub(input_file)
        source_lang = EpubTools.get_default_language(book)
        self.logger.console(f"📖 源语言: {source_lang}")

        # 2. 准备缓存
        cache_key = None
        progress = None
        glossary = {}

        if self.cache_manager:
            cache_key = self.cache_manager.get_cache_key(input_file, target_language)

            if resume:
                progress = self.cache_manager.load_progress(cache_key)

                if progress:
                    self.logger.console(
                        f"♻️  发现缓存，已完成 {len(progress.completed_chapters)}/{progress.total_chapters} 章节"
                    )
                    glossary = progress.glossary

        # 3. 初始化进度
        chapters = EpubTools.get_all_chapters(book)
        self.logger.console(f"\n共{len(chapters)}章...\n")

        if not progress:
            progress = TranslationProgress(
                source_lang=source_lang,
                target_lang=target_language,
                total_chapters=len(chapters),
            )
            # 保存初始进度（如果启用缓存）
            if self.cache_manager and cache_key:
                self.cache_manager.save_progress(cache_key, progress)

        # 4. 创建上下文
        ctx = EpubContext(
            book=book,
            target_language=target_language,
            cache_key=cache_key,
            cache_manager=self.cache_manager,
            glossary=glossary,
        )

        # 5. 把缓存中已翻译的章节内容回填到 book，
        #    否则续译产出的 EPUB 里这些章节仍是原文
        if resume and cache_key:
            self._restore_cached_chapters(ctx, progress, cache_key)

        # 6. 生成输出路径
        output_file = self._generate_output_path(input_file, target_language)

        self.logger.console("\n🤖 启动智能翻译 Agent...\n")

        # 7. 由 Python 控制章节循环，每章一次独立 run，避免上下文累积
        pending = self._pending_chapters(ctx, progress)
        if not pending:
            self.logger.console("所有章节均已翻译，直接生成文件")

        for position, (index, chapter) in enumerate(pending, 1):
            title = chapter.get_name() or chapter.get_id()
            self.logger.console(f"\n[{position}/{len(pending)}] 章节 {index}: {title}")
            ok = await self._translate_chapter_with_retry(ctx, index)
            if not ok:
                self._mark_failed(ctx, chapter.get_id())  # type: ignore

        # 8. 目录翻译
        if translate_toc:
            await self._run_toc_translation(ctx)

        # 9. 图片翻译
        if translate_images:
            await self._run_image_translation(ctx)

        # 10. 由 Python 收尾写盘，不依赖 Agent 是否记得调用
        return self._finalize_and_report(ctx, output_file)

    def _restore_cached_chapters(
        self, ctx: EpubContext, progress: TranslationProgress, cache_key: str
    ) -> None:
        """把缓存里已完成章节的译文写回 book 对象"""
        if not self.cache_manager:
            return

        restored = 0
        missing = []
        for chapter in ctx.chapters:
            chapter_id = chapter.get_id()
            if chapter_id not in progress.completed_chapters:
                continue
            cached = self.cache_manager.load_chapter(cache_key, chapter_id)
            if cached:
                chapter.set_content(cached.encode("utf-8"))
                restored += 1
            else:
                missing.append(chapter_id)

        if restored:
            self.logger.console(f"♻️  已从缓存恢复 {restored} 个章节的译文")
        if missing:
            # 进度记录说已完成但译文文件丢失，需要重新翻译，否则会静默输出原文
            self.logger.console(
                f"⚠️  {len(missing)} 个章节标记为已完成但缓存内容缺失，将重新翻译"
            )
            for chapter_id in missing:
                progress.completed_chapters.remove(chapter_id)
            self.cache_manager.save_progress(cache_key, progress)

    def _pending_chapters(self, ctx: EpubContext, progress: TranslationProgress):
        """返回待翻译章节的 (索引, 章节对象) 列表，索引从 1 开始"""
        return [
            (idx, chapter)
            for idx, chapter in enumerate(ctx.chapters, 1)
            if chapter.get_id() not in progress.completed_chapters
        ]

    def _is_chapter_done(self, ctx: EpubContext, chapter_index: int) -> bool:
        """以缓存进度为准判断某章是否真的保存成功"""
        if not self.cache_manager or not ctx.cache_key:
            # 未启用缓存时退而求其次：prepare_chapter 会填入待翻译队列，
            # 只有 save_translated_chapter 成功执行才会把它移除
            return chapter_index not in ctx.untranslated_buffer
        progress = self.cache_manager.load_progress(ctx.cache_key)
        if not progress:
            return False
        return ctx.chapters[chapter_index - 1].get_id() in progress.completed_chapters

    async def _translate_chapter_with_retry(
        self, ctx: EpubContext, chapter_index: int
    ) -> bool:
        """翻译单个章节，失败则重试。返回是否成功保存"""
        logger = get_logger()
        title = ctx.chapters[chapter_index - 1].get_name() or ""

        for attempt in range(1, MAX_CHAPTER_RETRIES + 2):
            if attempt > 1:
                logger.console(f"  ↻ 第 {attempt} 次尝试")
            logger.chapter_start(chapter_index, title, attempt)

            # 切分由 Python 完成，不交给模型：模型重复切分会清空已攒的译文
            chunk_count = ctx.prepare_chapter(chapter_index)
            if chunk_count == 0:
                logger.error(f"章节 {chapter_index} 内容为空，跳过")
                return False
            logger.console(f"  切分为 {chunk_count} 块")

            try:
                result = await self.agent.run(
                    self._build_chapter_task(ctx, chapter_index, chunk_count),
                    deps=ctx,
                    usage_limits=UsageLimits(request_limit=MAX_REQUESTS),
                )  # type: ignore
            except Exception as e:
                logger.error(
                    f"章节 {chapter_index} 第 {attempt} 次尝试抛异常 "
                    f"{type(e).__name__}: {e}"
                )
                continue

            logger.run_result(chapter_index, result)

            output = result.output or ""
            if any(marker in output for marker in _LEAKED_TOOL_CALL_MARKERS):
                # 模型把工具调用写成了纯文本，本轮实际没有落盘
                logger.leaked_tool_call(chapter_index, output)
                continue

            if self._is_chapter_done(ctx, chapter_index):
                logger.info(f"章节 {chapter_index} 第 {attempt} 次尝试保存成功")
                return True

            logger.error(
                f"章节 {chapter_index} run 正常结束但未落盘，"
                f"剩余未取分块 {len(ctx.untranslated_buffer.get(chapter_index, []))}，"
                f"缓冲区 {len(ctx.translation_buffer.get(chapter_index, ''))} 字符"
            )

        logger.json_line(
            {"event": "chapter_failed", "chapter": chapter_index, "title": title}
        )
        return False

    def _mark_failed(self, ctx: EpubContext, chapter_id: str) -> None:
        """把章节记入失败列表"""
        if not self.cache_manager or not ctx.cache_key:
            return
        progress = self.cache_manager.load_progress(ctx.cache_key)
        if progress and chapter_id not in progress.failed_chapters:
            progress.failed_chapters.append(chapter_id)
            self.cache_manager.save_progress(ctx.cache_key, progress)

    def _build_chapter_task(
        self, ctx: EpubContext, chapter_index: int, chunk_count: int
    ) -> str:
        """构建单章翻译任务提示词"""
        chapter = ctx.chapters[chapter_index - 1]
        title = chapter.get_name() or chapter.get_id()

        glossary_hint = ""
        if ctx.glossary:
            # 只带少量术语，避免提示词随书变长
            terms = list(ctx.glossary.items())[:40]
            pairs = "、".join(f"{k}→{v}" for k, v in terms)
            glossary_hint = f"\n## 已有术语（请保持一致）\n{pairs}\n"

        return f"""\
请翻译第 {chapter_index} 章：{title}

源语言：{ctx.source_language}　目标语言：{ctx.target_language}
本章已切分为 {chunk_count} 个待翻译分块。
{glossary_hint}
## 执行步骤

重复以下循环 {chunk_count} 次，每次处理一个分块：

1. 调用 get_untranslated_content({chapter_index}) 取出一块原文
2. 翻译这一块，只翻译文本，完整保留所有 HTML 标签和属性
3. 调用 store_translation_chunk({chapter_index}, 译文) 写入译文；
   内容长时可拆成多次调用，工具会自动按顺序拼接

工具返回值会告诉你还剩几块。全部取完后：

4. 可选：发现新的人名、地名、专有名词时调用 update_glossary 记录
5. 调用 save_translated_chapter({chapter_index}) 保存本章

## 注意

- 本次任务只处理第 {chapter_index} 章，保存成功后就结束
- 每个分块都必须完整翻译，不要省略或概括原文内容
- 分块只发放一次，取出后无法重新获取，务必立刻翻译并写入
- 必须通过工具调用机制操作，不要把工具调用写成文本
"""

    async def _run_toc_translation(self, ctx: EpubContext) -> None:
        """单独一次 run 处理目录翻译"""
        if self.cache_manager and ctx.cache_key:
            progress = self.cache_manager.load_progress(ctx.cache_key)
            if progress and progress.toc_translated:
                # book.toc 每次都从原书重读，只跳过不回填的话目录会退回原文
                if progress.toc_titles and self._restore_cached_toc(
                    ctx, progress.toc_titles
                ):
                    return
                self.logger.console("\n目录缓存不可用，重新翻译")

        self.logger.console("\n📑 翻译目录...")
        prompt = f"""\
请翻译本书目录到 {ctx.target_language}。

1. 调用 translate_toc 获取所有目录项
2. 按**完全相同的顺序和数量**翻译这些标题，保持与正文术语一致
3. 调用 save_translated_toc(译后标题列表) 保存

只处理目录，不要翻译章节正文。
"""
        try:
            await self.agent.run(
                prompt, deps=ctx, usage_limits=UsageLimits(request_limit=MAX_REQUESTS)
            )  # type: ignore
        except Exception as e:
            self.logger.error(f"目录翻译失败 {type(e).__name__}: {e}")

    def _restore_cached_toc(self, ctx: EpubContext, cached_titles: list) -> bool:
        """把缓存的目录译文写回 book.toc 和导航文档，返回是否成功"""
        original_titles = collect_toc_titles(ctx.book.toc)
        if len(cached_titles) != len(original_titles):
            get_logger().error(
                f"缓存目录条目数不符：{len(cached_titles)}/{len(original_titles)}"
            )
            return False

        ctx.book.toc = apply_toc_titles(ctx.book.toc, iter(cached_titles))
        nav_updated = sync_nav_documents(ctx, original_titles, cached_titles)
        self.logger.console(f"\n♻️  已从缓存恢复目录译文（侧边栏同步 {nav_updated} 条）")
        return True

    async def _run_image_translation(self, ctx: EpubContext) -> None:
        """单独一次 run 处理图片翻译"""
        self.logger.console("\n🖼️  翻译图片...")
        prompt = f"""\
请翻译本书图片中的文字到 {ctx.target_language}。

1. 调用 list_images 查看所有图片
2. 对于含文字的图片：get_image_base64 获取 → 识别并翻译文字 →
   生成替换文字后的图片 → save_translated_image 保存
3. 无法处理的图片直接跳过

只处理图片，不要翻译章节正文。
"""
        try:
            await self.agent.run(
                prompt, deps=ctx, usage_limits=UsageLimits(request_limit=MAX_REQUESTS)
            )  # type: ignore
        except Exception as e:
            self.logger.error(f"图片翻译失败 {type(e).__name__}: {e}")

    def _finalize_and_report(self, ctx: EpubContext, output_file: str) -> str:
        """写盘并如实报告完成情况"""
        completed = 0
        total = len(ctx.chapters)
        failed = []

        if self.cache_manager and ctx.cache_key:
            progress = self.cache_manager.load_progress(ctx.cache_key)
            if progress:
                completed = len(progress.completed_chapters)
                total = progress.total_chapters
                failed = progress.failed_chapters

        logger = get_logger()
        logger.console()
        logger.console(finalize_epub(ctx, output_file))

        logger.json_line(
            {
                "event": "finish",
                "completed": completed,
                "total": total,
                "failed": failed,
                "output": output_file,
            }
        )

        if completed < total:
            logger.console(f"\n⚠️  翻译未全部完成：{completed}/{total} 章")
            if failed:
                logger.console(f"   失败章节 {len(failed)} 个: {', '.join(failed[:10])}")
            logger.console("   已生成的文件中，未完成章节仍是原文")
            logger.console("   可重新运行相同命令（带 --resume）继续翻译剩余章节")
        else:
            logger.console(f"\n✅ 翻译完成：{completed}/{total} 章")

        logger.console(f"📄 输出文件: {output_file}")
        return output_file

    def _generate_output_path(self, input_file: str, target_language: str) -> str:
        """生成输出文件路径"""
        path = Path(input_file)
        return str(path.parent / f"{path.stem}({target_language}){path.suffix}")
