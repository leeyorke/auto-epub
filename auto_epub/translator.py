"""
EPUB 翻译器 - 使用 Agent + Toolsets 方式
"""

from pathlib import Path

from ebooklib import epub
from pydantic_ai import Agent

from .agent_tools import EpubContext
from .cache_manager import CacheManager
from .epub_tools import EpubTools
from .models import TranslationProgress


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
        result = await self.agent.run(task_prompt, deps=ctx)  # type: ignore

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

**翻译目标：**
- 源语言: {progress.source_lang}
- 目标语言: {progress.target_lang}
- 输出文件: {output_file}

**任务流程：**

**获取书籍信息**
   - 使用 get_book_info 工具查看书籍基本信息
   - 使用 list_chapters 工具查看所有章节

2. **翻译章节**（最重要的任务）
   对于每个待翻译的章节：
   - 使用 get_chapter_content 获取章节内容
   - 翻译 HTML 内容中的文本，保留所有 HTML 标签
   - 识别并记录专有名词（人名、地名等）到术语表
   - 使用 save_translated_chapter 保存翻译结果
   - 发现新术语时使用 update_glossary 更新术语表

   **翻译规则：**
   - 只翻译文本内容，完全保留 HTML 标签和属性
   - 专有名词第一次出现时标注原文，如：于连·索雷尔(Julien Sorel)
   - 后续出现保持一致性，使用术语表
   - 保持文学风格和语气
   - 段落结构不变

3. **检查进度**
   - 每翻译完一个章节就使用 get_translation_progress 查看下进度
   - 确保所有章节都已翻译
"""

        if translate_toc:
            task += """
4. **翻译目录**
   - 使用 translate_toc 获取目录项
   - 翻译所有目录标题（保持与正文术语一致）
   - 使用 save_translated_toc 保存翻译后的目录
"""

        if translate_images:
            task += """
5. **翻译图片**
   - 使用 list_images 查看所有图片
   - 对于有文字的图片：
     * 使用 get_image_base64 获取图片
     * 识别图片中的文字并翻译
     * 生成新图片（文字替换为译文）
     * 使用 save_translated_image 保存
"""

        task += f"""

6. **完成翻译**
   - 使用 finalize_epub 保存最终的 EPUB 文件到 {output_file}

**当前进度：**
- 已完成章节: {len(progress.completed_chapters)}
- 总章节数: {progress.total_chapters}
- 失败章节: {len(progress.failed_chapters)}

**重要提示：**
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
