# 文件对照表

## 核心文件清单

| 文件名 | 说明 | 主要内容 |
|--------|------|----------|
| **auto_epub/chunk_translator.py** | 块级翻译核心（**章节正文走这里，无工具**） | `translate_chapter_chunks` / `translate_one_chunk`、接力包 `build_carryover` / `build_chunk_prompt`、输出清理 `clean_model_html` / `split_terms_block` / `parse_terms_block`、块级校验 `validate_chunk` |
| **auto_epub/agent_tools.py** | 目录/图片工具集 + 共享上下文 | `epub_toolset` (FunctionToolset)、`EpubContext`、10 个工具函数、标签计数辅助 `_count_tags` / `_missing_tag_names`、非工具函数 `finalize_chapter` / `finalize_epub` / `merge_glossary` / `collect_toc_titles` / `apply_toc_titles` / `sync_nav_documents` |
| **auto_epub/translator.py** | 翻译编排器 | `EpubTranslator`：逐章循环、`_translate_chapter_with_retry`（章级重试）、`_run_agent`（目录/图片）、`_restore_cached_toc`、收尾写盘 |
| **auto_epub/epub_tools.py** | EPUB 底层工具 | `EpubTools` 静态类：语言检测、章节提取、递归分块 `_atomize`/`split_html_content`、token 计数、导航文档识别 `find_nav_documents`、标题替换 `apply_nav_labels` |
| **auto_epub/logger.py** | 诊断日志 | `TranslationLogger` + 模块级单例 `get_logger()` / `init_logger()`，记录分块尺寸、**每块每次尝试一行**（`chunk_result`）、run 用量（`run_result`）、失败原因与被拒译文转储 |
| **auto_epub/cache_manager.py** | 缓存管理器 | `CacheManager`：进度/章节/**块**/图片的存取（默认目录 `~/.auto-epub/cache`），缓存键 = md5(文件绝对路径 + 目标语言)，块缓存按 md5(块原文) 内容寻址 |
| **auto_epub/models.py** | 数据模型 | `TranslationProgress`（含 `toc_translated`、`toc_titles`）、`ChapterTranslation`、`ImageTranslationResult`、`TranslationResult` |
| **auto_epub/client.py** | Agent 工厂 | `_build_model`（共享 Model/Provider/Settings）、`create_epub_agent`（带 toolset）、`create_chunk_agent`（**无工具**）、`create_translator` |
| **auto_epub/cli.py** | 命令行接口 | Typer app：`translate`、`clear`（省略书籍路径时清空全部缓存）、`version` |
| **auto_epub/config.py** | 配置加载 | 从 `.env` 加载 API 配置（base_url / api_key / model） |
| **auto_epub/settings.py** | 常量配置 + 系统提示词 | `CHUNK_SYSTEM_PROMPT`（块级，无工具）、`AGENT_SYSTEM_PROMPT`（目录/图片）、应用数据目录（`APP_DIR`/`CACHE_DIR`/`LOG_DIR` 与旧目录迁移 `migrate_legacy_dir`）、token 限制、重试次数、接力包预算、标签比例阈值、功能开关、日志开关 |
| **auto_epub/concurrent_manager.py** | 并发控制器 | asyncio 并发 + 速率限制，**当前未被主流程使用** |
| **auto_epub/__init__.py** | 包初始化 | 导出主要类和函数，`__version__` |

## 配置和文档

| 文件名 | 说明 |
|--------|------|
| **main.py** | CLI 入口 |
| **example.py** | 编程式调用示例 |
| **.env.example** | 环境变量模板 |
| **requirements.txt** | 完整锁定依赖（uv 导出） |
| **pyproject.toml** | 项目配置（uv / ruff） |
| **README.md** | 完整使用文档 |
| **CLAUDE.md** | 面向 Claude Code 的代码库指南（只放命令、导航、红线索引） |
| **docs/QUICKSTART.md** | 快速开始指南 |
| **docs/ARCHITECTURE.md** | 架构设计文档 —— 设计决策、不变量、诊断日志的唯一出处 |

## 重要区分

### chunk_translator.py vs agent_tools.py

**chunk_translator.py（章节正文，无工具）**
- 一块原文 → 一次 `chunk_agent.run`（`settings.STREAMING=True` 时为 `run_stream` 流式）→ 一块译文，模型没有任何工具可调
- 上下文靠接力包拼进 prompt，不靠 message history（单请求输入与块序号无关）
- 块级校验、块级重试计数、块缓存读写都在这里

**agent_tools.py（目录/图片工具集）**
- 定义 `epub_toolset`（FunctionToolset）和 `EpubContext`（工具型 Agent 的 deps）
- 10 个工具函数，供 Agent 调用，使用 `@epub_toolset.tool` 装饰器
- **章节级工具已全部下线**，职责回到 Python 侧
- `finalize_chapter` / `finalize_epub` / `merge_glossary` 是普通函数而非工具，但也定义在此（与工具共享 `EpubContext` 和标签计数辅助）

### epub_tools.py vs agent_tools.py

**epub_tools.py（EPUB 底层工具）**
- 静态工具类 `EpubTools`
- EPUB 文件操作的底层方法：语言检测、章节提取、递归 HTML 分块、token 计数、导航文档读写
- 不依赖 pydantic-ai，可以独立使用

### 使用关系

```
EpubTranslator（章节循环）
  ├─ chunk_translator.py（块循环）
  │    ↓ chunk_agent.run()（STREAMING=False 默认；True 时 run_stream），无工具
  │  agent_tools.py 的非工具部分（finalize_chapter、_count_tags、merge_glossary）
  │
  └─ 目录 / 图片各一次 agent.run(deps=EpubContext)
       ↓ 调用
     agent_tools.py（epub_toolset 工具函数）
       ↓ 内部使用
     epub_tools.py（EpubTools 静态类）
       ↓ 操作
     EPUB 文件
```

工具函数与 EpubTools 之间没有一对一关系：一个工具可能组合多个底层方法（如 `save_translated_toc` 用 `collect_toc_titles` + `apply_toc_titles` + `sync_nav_documents` + `EpubTools.find_nav_documents`）。

## 调用链示例

### 翻译一个章节（每块一次独立 run）

```
translator._translate_chapter_with_retry() (translator.py)
  ↓
ctx.prepare_chapter(n)         # Python 侧切分，只做一次；chunk_attempts 不重置
  ↓
translate_chapter_chunks(chunk_agent, ctx, n, total)  (chunk_translator.py)
  └─ 按块号顺序 translate_one_chunk()
       ├─ cache_manager.load_chunk(key, chapter_id, 块原文)   # 命中就不发 API
       ├─ has_translatable_text() 为假 → 原样透传，也不发 API
       ├─ build_chunk_prompt = 原文 + build_carryover（接缝 / 术语 / 风格锚点，≤2000 tokens）
       ├─ chunk_agent.run(prompt)   # 无工具、无 deps、无 history；非流式收全文
       ├─ clean_model_html → split_terms_block → parse_terms_block
       ├─ validate_chunk（块级标签比例、工具调用泄漏、finish_reason=length）
       │    ├─ 通过 → 写 chunk_translations[n][i] + save_chunk 落缓存 + merge_glossary
       │    └─ 不通过 → dump_chunk 转储原始输出，重试（每块共 3 次）
       └─ logger.chunk_result(...)         # 每块每次尝试一行，含"额度已用尽"
  ↓
finalize_chapter(ctx, n)  (agent_tools.py，非工具)
  ├─ 校验 1：pending_chunks 是否为空（有块没译文就硬拦）
  ├─ 校验 2：全章块级标签比例 ≥ MIN_BLOCK_TAG_RATIO
  ├─ chapter.set_content()          # 写回 book 对象（判定不完整也写）
  ├─ cache_manager.save_chapter()   # 落盘缓存
  ├─ cache_manager.save_progress()  # 更新 completed_chapters（仅完整时）
  └─ return (是否完整, 说明)        # 判定不回读进度文件
  ↓
章级重试：只清 thin_chunks 的译文，坏块块级额度用尽则提前收工
```

### 翻译目录

```
translator 目录阶段（单次 run）
  ↓
translate_toc → collect_toc_titles(book.toc)   # 摊平目录标题
  ↓
save_translated_toc(译文列表)
  ├─ 数量校验（防错位）
  ├─ apply_toc_titles → book.toc        # toc.ncx 由此生成
  ├─ sync_nav_documents → EpubTools.find_nav_documents
  │     → EpubTools.apply_nav_labels    # 侧边栏 nav.xhtml
  └─ progress.toc_titles = 译文          # 续译时回填
```

## 目录结构

```
auto-epub/
├── auto_epub/
│   ├── __init__.py           ← 包初始化
│   ├── models.py             ← Pydantic 数据模型
│   ├── epub_tools.py         ← EPUB 底层工具（静态类）
│   ├── agent_tools.py        ← 目录/图片工具集 + EpubContext + finalize_chapter
│   ├── chunk_translator.py   ← 块级翻译核心（章节正文，无工具）
│   ├── translator.py         ← 翻译编排器
│   ├── client.py             ← Agent 工厂
│   ├── logger.py             ← 诊断日志
│   ├── cache_manager.py      ← 缓存管理
│   ├── concurrent_manager.py ← 并发控制器（未使用）
│   ├── cli.py                ← 命令行接口
│   ├── config.py             ← 配置加载
│   └── settings.py           ← 常量配置 + 系统提示词
├── docs/                     ← 文档（本目录）
├── main.py                   ← CLI 入口
├── example.py                ← 编程式调用示例
├── .env.example              ← 环境变量模板
├── requirements.txt          ← 锁定依赖
├── pyproject.toml            ← 项目配置
├── CLAUDE.md                 ← 代码库指南
└── README.md                 ← 完整文档
```

## 文件规模

| 文件 | 行数 | 说明 |
|------|------|------|
| config.py | 25 | 配置加载 |
| __init__.py | 35 | 包初始化 |
| models.py | 54 | 数据模型 |
| concurrent_manager.py | 120 | 并发控制（未使用） |
| client.py | 124 | Agent 工厂（两个 Agent） |
| cli.py | 174 | 命令行接口 |
| settings.py | 183 | 常量 + 两份提示词 |
| cache_manager.py | 212 | 缓存管理（含块缓存） |
| epub_tools.py | 264 | EPUB 底层工具 |
| logger.py | 422 | 诊断日志 |
| translator.py | 427 | 翻译编排器 |
| chunk_translator.py | 506 | 块级翻译核心 |
| agent_tools.py | 809 | 工具集 + 上下文 + finalize |

总计约 3350 行。
