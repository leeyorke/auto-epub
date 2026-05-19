# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在处理此存储库中的代码时提供指导。

## 常用命令

```bash
# 安装依赖
uv sync

# 运行翻译（CLI）
python main.py translate book.epub -l zh

# 运行示例脚本
python example.py

# 代码检查
ruff check .

# 代码格式化
ruff format .
```

## 高等级架构

### 核心定位与技术栈

EPUB 电子书多语言翻译工具，基于 **pydantic-ai** 的 **Agent + FunctionToolset** 架构，让 LLM Agent 自主调度 EPUB 操作工具完成逐章翻译、术语管理、缓存续译等任务。

**技术栈**: Python >= 3.10, pydantic-ai (Agent 框架), ebooklib (EPUB 解析), BeautifulSoup4 (HTML 操作), typer (CLI), tiktoken (token 计数), ruff (代码检查), uv (包管理)

### 目录结构

```
auto_epub/           # 核心模块
├── agent_tools.py   # Agent 工具集（FunctionToolset），所有可被 Agent 调用的工具在此定义
├── translator.py    # 翻译编排器 EpubTranslator，加载 EPUB、构建任务提示词、驱动 Agent.run
├── client.py        # Agent 创建工厂，组装 Model/Provider/Settings/Toolsets 为 Agent 实例
├── epub_tools.py    # EPUB 文件底层操作（语言检测、章节提取、HTML 分块、token 计数）
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

1. **Agent-driven orchestration**: 不是传统的函数调用链，而是由 LLM Agent 根据任务提示词自主决定调用哪些工具、按什么顺序执行。Translator 只负责加载数据和构建任务描述，不控制具体翻译流程。
2. **Toolsets 模式**: 使用 pydantic-ai 的 `FunctionToolset` 将所有 EPUB 操作注册为 Agent 可调用的工具，每个工具有独立的 docstring 供 LLM 理解用途。
3. **上下文即状态**: 所有可变状态（book 对象、术语表、缓存等）集中在 `EpubContext` 中作为 Agent 的 `deps` 传入，工具通过 `RunContext.deps` 访问共享状态。
4. **缓存驱动恢复**: 翻译进度序列化为 JSON 存储在 `.epub_translation_cache/`，章节内容和图片分开缓存。缓存键基于文件绝对路径+目标语言的 MD5。

### 核心数据流

```
CLI (typer) → EpubTranslator.translate_epub()
  ├── 读取 EPUB → EpubContext（含 book, chapters, images, glossary）
  ├── 加载缓存进度（如有）
  ├── 构建任务提示词（含翻译规则、当前进度、工具调用指引）
  └── Agent.run(task_prompt, deps=EpubContext)
        └── Agent 自主循环：
              ├── get_chapter_content → LLM 翻译 → save_translated_chapter
              ├── update_glossary / get_glossary（术语管理）
              ├── translate_toc / save_translated_toc（目录翻译）
              ├── get_image_base64 → LLM 翻译 → save_translated_image（图片翻译）
              └── finalize_epub（写入最终 EPUB 文件）
```

### 关键设计决策

- **settings.py 同时承载配置和提示词**: 将 Agent 系统提示词放在 settings.py 而非单独文件，便于用户直接修改翻译规则和风格。系统提示词中的 `{target_language}` 在 client.py 中通过 `str.format()` 注入。
- **Agent 单次 run 完成全流程**: 不是为每个章节创建独立的 Agent run，而是构建一个完整任务提示词让 Agent 一次 run 内完成所有章节翻译。这使得 Agent 可以维护跨章节的上下文（术语一致性）。
- **concurrent_manager.py 当前未使用**: 该模块提供了 asyncio 并发控制和速率限制能力，但当前翻译流程是串行的（Agent 逐章翻译），如果需要并行翻译多本书可以引入此模块。
- **deepseek 兼容**: client.py 中显式设置 `extra_body={"thinking": {"type": "disabled"}}`，是为兼容 deepseek 等需要禁用思考模式的模型。

### 项目特有约定

- **依赖管理**: 使用 uv，镜像源为清华大学 PyPI 镜像（在 pyproject.toml 中配置）
- **API 配置**: 通过 `.env` 文件加载，支持任意兼容 OpenAI API 格式的供应商（包含 base_url, api_key, model）
- **版本**: 定义在 `auto_epub/__init__.py` 的 `__version__`
- **缓存清理**: 通过 CLI 命令 `python main.py clear-cache <file> -l <lang>` 清理
- **无测试文件**: 当前没有单元测试或集成测试
