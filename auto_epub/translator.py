"""
EPUB 翻译器 - 章节循环由 Python 编排，块级翻译走无工具 Agent
"""

from pathlib import Path
from typing import Optional, Union

from ebooklib import epub
from pydantic_ai import Agent, AgentRunResult, UsageLimits

from .agent_tools import (
    EpubContext,
    apply_toc_titles,
    collect_toc_titles,
    finalize_chapter,
    finalize_epub,
    sync_nav_documents,
)
from .cache_manager import CacheManager
from .chunk_translator import LEAKED_TOOL_CALL_MARKERS, translate_chapter_chunks
from .epub_tools import EpubTools
from .logger import ConsoleLevel, get_logger, init_logger
from .models import TranslationProgress
from .settings import MAX_CHAPTER_RETRIES, MAX_CHUNK_RETRIES, MAX_REQUESTS


class EpubTranslator:
    """EPUB 翻译器

    章节正文由 Python 逐块喂给无工具的 chunk_agent（每块一次独立 run），
    目录与图片仍交给带工具集的 agent 自主调度。
    """

    def __init__(
        self,
        agent: Agent[EpubContext, str],
        chunk_agent: Optional[Agent[None, str]] = None,
        cache_enabled: bool = True,
    ):
        """
        Args:
            agent: 带 toolsets 的 Agent，服务目录与图片阶段
            chunk_agent: 无工具的块级翻译 Agent；为 None 时在 translate_epub
                里按目标语言现建一个（兼容 EpubTranslator(agent=...) 的旧写法）
            cache_enabled: 是否启用缓存
        """
        self.agent = agent
        self.chunk_agent = chunk_agent
        self.cache_manager = CacheManager() if cache_enabled else None

    def _resolve_chunk_agent(self, target_language: str) -> Agent[None, str]:
        """拿到块级 Agent，调用方没给就现建一个

        延迟导入是为了避开 client → translator → client 的循环导入：
        client 需要 EpubTranslator 才能组装翻译器，所以模块级不能反向引用它。
        """
        if self.chunk_agent is None:
            from .client import create_chunk_agent

            self.chunk_agent = create_chunk_agent(target_language)
        return self.chunk_agent

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
        book_name = Path(input_file).stem
        self.logger = init_logger(book_name, console_level=console_level)

        self.logger.console(f"📚 开始翻译 EPUB: {input_file}")
        self.logger.console(f"🎯 目标语言: {target_language}")
        self.logger.info(f"输入文件: {input_file}，目标语言: {target_language}")
        if self.logger.log_file:
            self.logger.console(f"📝 诊断日志: {self.logger.log_file}")

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
                    if progress.book_name != book_name:
                        # 旧缓存没有这个字段（或文件被改名），补上并立即落盘：
                        # 整本已翻完时后面不会再有 save_progress 把它写出去
                        progress.book_name = book_name
                        self.cache_manager.save_progress(cache_key, progress)

        # 3. 初始化进度
        chapters = EpubTools.get_all_chapters(book)
        self.logger.console(f"共{len(chapters)}章...")

        if not progress:
            progress = TranslationProgress(
                book_name=book_name,
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

        chunk_agent = self._resolve_chunk_agent(target_language)
        self.logger.console("🤖 启动翻译...")

        # 7. 由 Python 控制章节循环与块循环：每块一次独立 run，
        #    run 之间没有 message history，单请求输入与块序号无关
        pending = self._pending_chapters(ctx, progress)
        if not pending:
            self.logger.console("所有章节均已翻译，直接生成文件")

        for position, (index, chapter) in enumerate(pending, 1):
            title = chapter.get_name() or chapter.get_id()
            self.logger.console(f"[{position}/{len(pending)}] 章节 {index}: {title}")
            ok = await self._translate_chapter_with_retry(ctx, chunk_agent, index)
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

    async def _run_agent(
        self, prompt: str, ctx: EpubContext, stage: str
    ) -> AgentRunResult[str]:
        """跑一次工具型 Agent（目录 / 图片），并禁止工具并行执行

        pydantic-ai 会把同一个模型响应里的多个工具调用并发执行（同步工具进
        线程池，见 _agent_graph.py 的 should_call_sequentially 分支），而本项目
        的工具共享同一个 EpubContext 和同一个进度文件。实测 update_glossary
        与当时的 save_translated_chapter 落在同一个响应里时，两个写入者交错，
        进度文件被截成"短文档 + 旧尾巴"，此后整本书的缓存都读不出来。
        章节工具虽然已经下线，剩下的 update_glossary / save_translated_toc /
        save_translated_image 仍然都写进度文件，这个约束原样保留。
        """
        with self.agent.sequential_tool_calls():
            result = await self.agent.run(
                prompt, deps=ctx, usage_limits=UsageLimits(request_limit=MAX_REQUESTS)
            )

        # 这两个阶段以前完全没有诊断记录：run 正常返回但什么也没保存时，
        # 日志里一片空白（红线 9）。现在每次 run 都留下用量与结束原因。
        logger = get_logger()
        logger.run_result(stage, result)
        output = result.output or ""
        if any(marker in output for marker in LEAKED_TOOL_CALL_MARKERS):
            # 模型把工具调用写成了纯文本，本轮实际没有落盘
            logger.leaked_tool_call(stage, output)
        return result  # type: ignore[return-value]

    async def _translate_chapter_with_retry(
        self, ctx: EpubContext, chunk_agent: Agent[None, str], chapter_index: int
    ) -> bool:
        """逐块翻译一个章节并保存。返回是否完整保存

        重试主要发生在**块级**（chunk_translator 里每块最多 MAX_CHUNK_RETRIES + 1
        次），章级循环只兜住"坏块还有额度却整章没过"的情形。这两层不许相乘：
        块级次数跨章级重试累计，所以一个翻不动的坏块最多消耗固定的请求数。
        """
        logger = get_logger()
        chapter = ctx.chapters[chapter_index - 1]
        title = chapter.get_name() or ""

        # 切分只做一次：同一份原文切出来的块必然一样，重切没有收益，
        # 却会清空已经译好的块（关掉缓存时这些译文就真的白丢了）。
        logger.chapter_start(chapter_index, title, 1)
        total = ctx.prepare_chapter(chapter_index)
        if total == 0:
            logger.error(f"章节 {chapter_index} 内容为空，跳过")
            return False
        logger.console(f"切分为 {total} 块")

        for attempt in range(1, MAX_CHAPTER_RETRIES + 2):
            if attempt > 1:
                logger.console(f"↻ 第 {attempt} 次尝试")
                logger.chapter_start(chapter_index, title, attempt)
                # 只清掉判定漏译的块的译文，原文分块一律不动（红线 2）；
                # 通过校验的块保持原样，不重复花钱。
                for index in ctx.thin_chunks(chapter_index):
                    ctx.chunk_translations.get(chapter_index, {}).pop(index, None)

            passed, total = await translate_chapter_chunks(
                chunk_agent, ctx, chapter_index, total
            )
            ok, reason = finalize_chapter(ctx, chapter_index)
            if ok:
                logger.info(
                    f"章节 {chapter_index} 第 {attempt} 次尝试保存成功（{reason}）"
                )
                return True

            logger.error(
                f"章节 {chapter_index} 第 {attempt} 次尝试未通过：{reason}"
                f"（{passed}/{total} 块通过校验）"
            )

            # 整章不达标必然意味着有块没过（每块都达标则加总必然达标）。
            # 这些块的块级额度若已用尽，再来一轮章级重试只会原地空转。
            bad = set(ctx.pending_chunks(chapter_index)) | set(
                ctx.thin_chunks(chapter_index)
            )
            if all(ctx.attempts(chapter_index, i) > MAX_CHUNK_RETRIES for i in bad):
                logger.info(
                    f"章节 {chapter_index} 的 {len(bad)} 个坏块块级额度已用尽，"
                    f"章级重试不会有新结果，提前收工"
                )
                break

        logger.json_line(
            {"event": "chapter_failed", "chapter": chapter_index, "title": title}
        )
        return False

    def _mark_failed(self, ctx: EpubContext, chapter_id: str) -> None:
        """把章节记入失败列表"""
        if not self.cache_manager or not ctx.cache_key:
            return

        def _append(progress: TranslationProgress) -> None:
            if chapter_id not in progress.failed_chapters:
                progress.failed_chapters.append(chapter_id)

        self.cache_manager.update_progress(ctx.cache_key, _append)

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
                self.logger.console("目录缓存不可用，重新翻译")

        self.logger.console("📑 翻译目录...")
        prompt = f"""\
请翻译本书目录到 {ctx.target_language}。

1. 调用 translate_toc 获取所有目录项
2. 按**完全相同的顺序和数量**翻译这些标题，保持与正文术语一致
3. 调用 save_translated_toc(译后标题列表) 保存

只处理目录，不要翻译章节正文。
"""
        try:
            await self._run_agent(prompt, ctx, "目录翻译")
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
        self.logger.console(f"♻️  已从缓存恢复目录译文（侧边栏同步 {nav_updated} 条）")
        return True

    async def _run_image_translation(self, ctx: EpubContext) -> None:
        """单独一次 run 处理图片翻译"""
        self.logger.console("🖼️  翻译图片...")
        prompt = f"""\
请翻译本书图片中的文字到 {ctx.target_language}。

1. 调用 list_images 查看所有图片
2. 对于含文字的图片：get_image_base64 获取 → 识别并翻译文字 →
   生成替换文字后的图片 → save_translated_image 保存
3. 无法处理的图片直接跳过

只处理图片，不要翻译章节正文。
"""
        try:
            await self._run_agent(prompt, ctx, "图片翻译")
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
            logger.console(f"⚠️  翻译未全部完成：{completed}/{total} 章")
            if failed:
                logger.console(
                    f"   失败章节 {len(failed)} 个: {', '.join(failed[:10])}"
                )
            logger.console("已生成的文件中，未完成章节仍是原文或译文不完整")
            logger.console("可重新运行相同命令（带 --resume）继续翻译剩余章节")
        else:
            logger.console(f"✅ 翻译完成：{completed}/{total} 章")

        logger.console(f"📄 输出文件: {output_file}")
        return output_file

    def _generate_output_path(self, input_file: str, target_language: str) -> str:
        """生成输出文件路径"""
        path = Path(input_file)
        return str(path.parent / f"{path.stem}({target_language}){path.suffix}")
