# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在处理此存储库中的代码时提供指导。

## 常用命令

```bash
# 安装依赖
uv sync

# 运行翻译（CLI）
python main.py translate book.epub -l zh

# 续译（默认开启，可显式指定）
python main.py translate book.epub -l zh --resume

# 显示诊断细节（工具调用序列、被拒译文路径等）
python main.py translate book.epub -l zh -v

# 只输出错误（静默进度与摘要）
python main.py translate book.epub -l zh -q

# 清理缓存
python main.py clear-cache book.epub -l zh

# 运行示例脚本
python example.py

# 代码检查
ruff check .

# 代码格式化
ruff format .
```

## 高等级架构

### 核心定位与技术栈

EPUB 电子书多语言翻译工具，基于 **pydantic-ai** 的 **Agent + FunctionToolset** 架构。Python 负责流程编排与内容切分，LLM Agent 负责单章内的翻译与工具调度。

**技术栈**: Python >= 3.10, pydantic-ai (Agent 框架), ebooklib (EPUB 解析), BeautifulSoup4 + lxml (HTML/XML 操作), typer (CLI), tiktoken (token 计数), ruff (代码检查), uv (包管理)

### 目录结构

```
auto_epub/           # 核心模块
├── agent_tools.py   # Agent 工具集（FunctionToolset）+ EpubContext + 目录/导航同步辅助函数
├── translator.py    # 翻译编排器 EpubTranslator，控制章节循环、重试、目录/图片阶段、收尾写盘
├── client.py        # Agent 创建工厂，组装 Model/Provider/Settings/Toolsets 为 Agent 实例
├── epub_tools.py    # EPUB 底层操作（语言检测、章节提取、递归 HTML 分块、导航文档读写、token 计数）
├── logger.py        # 诊断日志，分块尺寸/token 用量/工具调用序列/失败原因落盘
├── cache_manager.py # 断点续传缓存管理，缓存键基于文件路径+目标语言的 MD5
├── concurrent_manager.py  # 并发控制器（带速率限制），当前未被主流程使用
├── models.py        # Pydantic 数据模型（TranslationProgress 等）
├── cli.py           # Typer CLI 入口，translate/clear-cache/version 三个命令
├── config.py        # .env 文件加载，提供 ModelProvider 配置
└── settings.py      # 全局常量配置 + Agent 系统提示词
main.py              # CLI 入口
example.py           # 编程式调用示例
```

### 架构设计原则

1. **Python 编排 + Agent 执行**: 章节循环、内容切分、重试、写盘时机全部由 Python 控制；Agent 只在"翻译一个章节"这一粒度上自主调度工具。早期版本让 Agent 在单次 run 内跑完整本书，会导致上下文无限累积、失败无法定位。
2. **Toolsets 模式**: 使用 pydantic-ai 的 `FunctionToolset` 将 EPUB 操作注册为 Agent 可调用的工具，每个工具有独立 docstring 供 LLM 理解用途。
3. **上下文即状态**: 所有可变状态（book 对象、术语表、分块队列、译文缓冲区等）集中在 `EpubContext` 中作为 Agent 的 `deps` 传入，工具通过 `RunContext.deps` 访问共享状态。
4. **缓存驱动恢复**: 翻译进度序列化为 JSON 存储在 `.epub_translation_cache/`，章节内容和图片分开缓存。缓存键基于文件绝对路径+目标语言的 MD5。

### 核心数据流

```
CLI (typer) → EpubTranslator.translate_epub()
  ├── init_logger → .epub_translation_logs/{书名}_{时间戳}.log
  ├── 读取 EPUB → EpubContext（含 book, chapters, images, glossary）
  ├── 加载缓存进度，并把已完成章节的译文回填进 book 对象
  │
  ├── for 每个未完成章节（Python 循环，每章一次独立 Agent run）:
  │     ├── ctx.prepare_chapter(index)   # Python 侧切分，记录原文标签数
  │     ├── agent.run(单章任务提示词, deps=ctx)
  │     │     └── Agent 循环：get_untranslated_content → 翻译
  │     │                    → store_translation_chunk（可多次）
  │     │                    → [update_glossary] → save_translated_chapter
  │     └── 以缓存进度校验本章是否真的落盘，失败则重试（MAX_CHAPTER_RETRIES）
  │
  ├── 目录阶段（单次 run）: translate_toc → save_translated_toc
  │     └── 同时写回 book.toc（toc.ncx）与 nav.xhtml（阅读器侧边栏）
  ├── 图片阶段（单次 run，默认关闭）: list_images → get_image_base64 → save_translated_image
  └── finalize_epub(ctx, output)  # 普通函数，不是 Agent 工具
```

### 关键设计决策与不变量

- **切分必须由 Python 完成，不能暴露给模型**（不变量）。`EpubContext.prepare_chapter` 在 run 之前把章节切好放进 `untranslated_buffer`。曾经把切分做成 Agent 工具，模型中途重新切分会清空 `translation_buffer` 里已攒的译文，导致该章永远保存不了。
- **分块器必须能下钻单根元素**。`EpubTools._atomize` 递归拆分：超过 `INPUT_MAX_TOKENS` 的元素先产出起始标签、递归处理内部、再补结束标签。早期版本只遍历 `body.children`，而 calibre 导出的 EPUB 常把整章包在一个 `<section>` 里，结果整章是一个 2.5 万 token 的块，输出被 `max_tokens` 截断 → 工具调用参数 JSON 不完整 → 模型退化成把 `<tool_call>` 当普通文本输出。
- **完整性校验用标签数而非字符数**（`MIN_TAG_RATIO`）。中文译文字符数天然比英文原文少一半左右，按字符判断会大量误报；HTML 标签数与语言无关。首次不达标拒绝并要求补译，第二次仍不达标则放行并告警，避免卡死在重试循环。
- **`INPUT_MAX_TOKENS` 必须显著小于 `OUTPUT_MAX_TOKENS`**。译文 + 完整 HTML 标签 + JSON 字符串转义叠加后，输出通常是输入的 1.5~2 倍。
- **侧边栏目录（nav.xhtml）要单独同步**。阅读器侧边栏来自 EPUB3 导航文档，不来自 `book.toc`；ebooklib 的 `EpubWriter._write_items` 只对 `EpubNcx`/`EpubNav` 实例重新生成内容，其余原样回写。calibre 导出的 EPUB 常没在 OPF 里标 `properties="nav"`，读进来只是普通 `EpubHtml`。因此用 `find_nav_documents` 按内容（`epub:type="toc"`）识别，再用 `apply_nav_labels` 替换 `<a>` 文本；page-list / landmarks 不翻译。
- **操作导航文档必须用 `xml` 解析器**。`BeautifulSoup(..., "html.parser")` 会把 XHTML 里的 `<head>` 内容丢掉，写回去的 nav.xhtml 会缺 `<title>` 和样式表链接。lxml 本身已是 ebooklib 依赖。
- **目录译文必须缓存到 `TranslationProgress.toc_titles`**。`book.toc` 每次都从原始 EPUB 重新读出，续译时若只看 `toc_translated` 布尔量就跳过翻译，产出的 toc.ncx 会退回原文。
- **写盘由 Python 收尾**。`finalize_epub` 是普通函数而非 Agent 工具，避免模型中途或漏章时提前落盘。
- **失败判定有两层**。一是输出里出现 `<tool_call` / `<function=` 等文本形式的工具调用标记；二是 run 正常结束但缓存进度里没有这一章。两者都触发重试。
- **settings.py 同时承载配置和提示词**: 系统提示词放在 settings.py 而非单独文件，便于直接修改翻译规则和风格。其中的 `{target_language}` 在 client.py 中通过 `str.format()` 注入。
- **concurrent_manager.py 当前未使用**: 该模块提供 asyncio 并发控制和速率限制能力，但当前翻译流程是串行的，如果需要并行翻译多本书可以引入。
- **deepseek 兼容**: client.py 中显式设置 `extra_body={"thinking": {"type": "disabled"}}`，兼容 deepseek 等需要禁用思考模式的模型。

### 诊断日志

翻译失败在控制台上往往只留一行错误。`logger.py` 把细节写入 `.epub_translation_logs/{书名}_{时间戳}.log`：

- 每章每次尝试的分隔行、逐块的 `tokens/chars/tags`
- 每次 run 的输出长度、输出片段、token 用量、实际发起的工具调用序列
- 文本形式工具调用的原始输出（用于判断是否为截断所致）
- 每次 save 被拒的原因；标签数不达标时把被拒译文另存为 `*_chN_rejected.html`
- `DATA` 前缀的 JSON 行（`save_chapter` / `chapter_failed` 等），便于脚本统计失败分布

`get_logger()` 是模块级单例——`agent_tools.py` 里的工具函数拿不到 translator 实例。控制台输出详细程度由 `ConsoleLevel` 分级控制（`QUIET/NORMAL/VERBOSE/DEBUG`）：CLI 用 `-v`（升到 DEBUG）/`-q`（降到 QUIET，只出错误）注入，编程式调用用 `set_console_level()` 或 `translate_epub(console_level=...)`；错误一律打到 stderr 且不受等级限制。**文件日志不受等级影响，始终记录完整信息。**

### 项目特有约定

- **依赖管理**: 使用 uv，镜像源为清华大学 PyPI 镜像（在 pyproject.toml 中配置）
- **API 配置**: 通过 `.env` 文件加载，支持任意兼容 OpenAI API 格式的供应商（包含 base_url, api_key, model）
- **版本**: 定义在 `auto_epub/__init__.py` 的 `__version__`
- **无测试文件**: 当前没有单元测试或集成测试

### 已知未解决问题

- **章节样式丢失**: `save_translated_chapter` 保存的是 body 级分块的拼接结果，丢掉了 `<html>`/`<head>` 外壳及其中的 CSS 链接。用户已明确表示暂缓处理。
