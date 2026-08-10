"""
EPUB 翻译器 - 使用 Agent + Toolsets 方式
"""

from pathlib import Path

from ebooklib import epub
from pydantic_ai import Agent, UsageLimits

from .agent_tools import EpubContext
from .cache_manager import CacheManager
from .epub_tools import EpubTools
from .models import TranslationProgress
from .settings import MAX_REQUESTS


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
    ) -> str:
        """
        翻译整个 EPUB 文件

        Args:
            input_file: 输入文件路径
            target_language: 目标语言代码
            translate_images: 是否翻译图片
            translate_toc: 是否翻译目录
            resume: 是否支持断点续传

        Returns:
            输出文件路径
        """
        print(f"\n📚 开始翻译 EPUB: {input_file}")
        print(f"🎯 目标语言: {target_language}\n")

        # 1. 加载 EPUB
        book = epub.read_epub(input_file)
        source_lang = EpubTools.get_default_language(book)
        print(f"📖 源语言: {source_lang}")

        # 2. 准备缓存
        cache_key = None
        progress = None
        glossary = {}

        if self.cache_manager:
            cache_key = self.cache_manager.get_cache_key(input_file, target_language)

            if resume:
                progress = self.cache_manager.load_progress(cache_key)

                if progress:
                    print(
                        f"♻️  发现缓存，已完成 {len(progress.completed_chapters)}/{progress.total_chapters} 章节"
                    )
                    glossary = progress.glossary

        # 3. 初始化进度
        chapters = EpubTools.get_all_chapters(book)
        print(f"\n共{len(chapters)}章...\n")

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

        # 5. 生成输出路径
        output_file = self._generate_output_path(input_file, target_language)

        # 6. 构建翻译任务提示词
        task_prompt = self._build_translation_task(
            progress=progress,
            translate_images=translate_images,
            translate_toc=translate_toc,
            output_file=output_file,
        )

        print("\n🤖 启动智能翻译 Agent...\n")

        # 7. 让 Agent 执行翻译
        result = await self.agent.run(
            task_prompt, deps=ctx, usage_limits=UsageLimits(request_limit=MAX_REQUESTS)
        )  # type: ignore

        print("\n✅ 翻译完成！")
        print(f"📄 输出文件: {output_file}")
        print(f"\n{result.output}")

        return output_file

    def _build_translation_task(
        self,
        progress: TranslationProgress,
        translate_images: bool,
        translate_toc: bool,
        output_file: str,
    ) -> str:
        """构建翻译任务提示词"""

        task = f"""\
请完成以下翻译任务：

## 翻译目标
- 源语言: {progress.source_lang}
- 目标语言: {progress.target_lang}
- 输出文件: {output_file}

## 任务流程

### 获取书籍信息
   - 使用 get_book_info 工具查看书籍基本信息
   - 使用 list_chapters 工具查看所有章节

### 翻译章节（严格按照以下步骤进行）

对于每个待翻译的章节：
   1. 使用 store_chapter_chunk 创建待翻译章节队列
   2. 使用 get_untranslated_content 获取待翻译内容片段
   3. 对获取的内容片段进行按照规则进行翻译
   4. 使用 store_translation_chunk 写入翻译结果（大章节可分多次调用）
   5. 使用 is_untranslated_buffer_empty 获取当前缓冲队列状态，如果为否，**重复2~5步骤**，否则就按找步骤顺序继续往下执行。
   6. 使用 save_translated_chapter 最终保存章节
   7. 发现新术语时使用 update_glossary 更新术语表
   8. 使用 get_translation_progress 查看进度


### 保存章节（两步法）
   1. 用 store_translation_chunk(chapter_index, translated_html) 写入翻译内容
       - 对内容较多的大章节可以分多次调用，每次传入部分内容
       - 工具会按章节索引自动拼接所有片段
   2. 最后用 save_translated_chapter(chapter_index) 一次性保存到 EPUB
       - 必须确保之前已通过 store_translation_chunk 写入了内容

### 检查进度
   - 每翻译完一个章节就使用 get_translation_progress 查看下进度
   - 确保所有章节都已翻译
"""

        if translate_toc:
            task += """
### 翻译目录
   - 使用 translate_toc 获取目录项
   - 翻译所有目录标题（保持与正文术语一致）
   - 使用 save_translated_toc 保存翻译后的目录
"""

        if translate_images:
            task += """
### 翻译图片
   - 使用 list_images 查看所有图片
   - 对于有文字的图片：
     * 使用 get_image_base64 获取图片
     * 识别图片中的文字并翻译
     * 生成新图片（文字替换为译文）
     * 使用 save_translated_image 保存
"""

        task += f"""

### 完成翻译

使用 finalize_epub 保存最终的 EPUB 文件到 {output_file}

在调用 finalize_epub 之前，必须按顺序执行：
1. 调用 list_chapters 获取总章节数
2. 调用 get_translation_progress 获取已完成章节数
3. 只有 已完成章节数 == 总章节数 时，才能调用 finalize_epub
4. 如果还有待翻译章节，继续翻译，严禁提前结束

### 当前进度
- 已完成章节: {len(progress.completed_chapters)}
- 总章节数: {progress.total_chapters}
- 失败章节: {len(progress.failed_chapters)}

### 重要提示
- 按顺序逐章翻译，不要遗漏
- 遇到错误时重试，但不要无限循环
- 完成后确认所有章节都已翻译
- 最后必须调用 finalize_epub 保存文件

开始执行任务！
"""

        return task

    def _generate_output_path(self, input_file: str, target_language: str) -> str:
        """生成输出文件路径"""
        path = Path(input_file)
        return str(path.parent / f"{path.stem}({target_language}){path.suffix}")
