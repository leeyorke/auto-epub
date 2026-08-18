# 文件对照表

## 核心文件清单

| 文件名 | 说明 | 主要内容 |
|--------|------|----------|
| **auto_epub/agent_tools.py** | Agent 工具集 + 共享上下文 | `epub_toolset` (FunctionToolset)、`EpubContext`、14 个工具函数、`collect_toc_titles` / `apply_toc_titles` / `sync_nav_documents` 辅助函数、`finalize_epub` |
| **auto_epub/translator.py** | 翻译编排器 | `EpubTranslator`：逐章循环、`_translate_chapter_with_retry`、目录/图片阶段、`_restore_cached_toc`、收尾写盘 |
| **auto_epub/epub_tools.py** | EPUB 底层工具 | `EpubTools` 静态类：语言检测、章节提取、递归分块 `_atomize`/`split_html_content`、token 计数、导航文档识别 `find_nav_documents`、标题替换 `apply_nav_labels` |
| **auto_epub/logger.py** | 诊断日志 | `TranslationLogger` + 模块级单例 `get_logger()` / `init_logger()`，记录分块尺寸、工具调用序列、失败原因 |
| **auto_epub/cache_manager.py** | 缓存管理器 | `CacheManager`：进度/章节/图片的存取，缓存键 = md5(文件绝对路径 + 目标语言) |
| **auto_epub/models.py** | 数据模型 | `TranslationProgress`（含 `toc_translated`、`toc_titles`）、`ChapterTranslation`、`ImageTranslationResult`、`TranslationResult` |
| **auto_epub/client.py** | Agent 工厂 | `create_epub_agent`（组装 Model/Provider/Toolset）、`create_translator` |
| **auto_epub/cli.py** | 命令行接口 | Typer app：`translate`、`clear-cache`、`version` |
| **auto_epub/config.py** | 配置加载 | 从 `.env` 加载 API 配置（base_url / api_key / model） |
| **auto_epub/settings.py** | 常量配置 + 系统提示词 | `AGENT_SYSTEM_PROMPT`、token 限制、重试次数、功能开关、日志开关 |
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

### epub_tools.py vs agent_tools.py

**epub_tools.py（EPUB 底层工具）**
- 静态工具类 `EpubTools`
- EPUB 文件操作的底层方法：语言检测、章节提取、递归 HTML 分块、token 计数、导航文档读写
- 不依赖 pydantic-ai，可以独立使用

**agent_tools.py（Agent 工具集）**
- 定义 `epub_toolset`（FunctionToolset）和 `EpubContext`（Agent 的 deps）
- 14 个工具函数，供 Agent 调用，使用 `@epub_toolset.tool` 装饰器
- 依赖 pydantic-ai
- `finalize_epub` 虽是普通函数而非工具，也定义在此（与工具共享 EpubContext）

### 使用关系

```
Agent（每章一次 run）
  ↓ 调用
agent_tools.py（epub_toolset 工具函数）
  ↓ 内部使用
epub_tools.py（EpubTools 静态类）
  ↓ 操作
EPUB 文件
```

工具函数与 EpubTools 之间没有一对一关系：一个工具可能组合多个底层方法（如 `save_translated_toc` 用 `collect_toc_titles` + `apply_toc_titles` + `sync_nav_documents` + `EpubTools.find_nav_documents`）。

## 调用链示例

### 翻译一个章节（单次 run 内）

```
translator._translate_chapter_with_retry() (translator.py)
  ↓
ctx.prepare_chapter(n)         # Python 侧切分，填充 chapter_chunks 并重置该章状态
  ↓
agent.run(单章提示词, deps=ctx)
  ├─ get_untranslated_content (agent_tools.py)
  │    ├─ 非破坏性发放：返回第一个 chunk_translations 里还没有译文的块
  │    └─ 都有译文时 → take_reissue_chunk：作废一个块级标签不达标的块的译文，
  │       重发它的原文（每块一次，chunk_reissued 记账）
  ├─ store_translation_chunk   # 按 chunk_index 写进 chunk_translations，同块追加
  │    └─ 校验该块块级标签比例 ≥ MIN_BLOCK_TAG_RATIO，内联不足只点名
  ├─ update_glossary           # 写 glossary + 同步缓存进度
  └─ save_translated_chapter
       ├─ 校验 1：pending_chunks 是否为空（有块没译文就硬拦）
       ├─ 校验 2：全章块级标签比例 ≥ MIN_BLOCK_TAG_RATIO
       ├─ chapter.set_content()          # 写回 book 对象（判定不完整也写）
       ├─ cache_manager.save_chapter()   # 落盘缓存
       ├─ cache_manager.save_progress()  # 更新 completed_chapters（仅完整时）
       └─ finished_chapters.add(n)       # 此后本章所有章节级工具只回收尾指令
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
│   ├── agent_tools.py        ← Agent 工具集 + EpubContext
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
| __init__.py | 34 | 包初始化 |
| models.py | 54 | 数据模型 |
| client.py | 80 | Agent 工厂 |
| settings.py | 88 | 常量 + 提示词 |
| concurrent_manager.py | 120 | 并发控制（未使用） |
| cli.py | 174 | 命令行接口 |
| cache_manager.py | 176 | 缓存管理 |
| epub_tools.py | 264 | EPUB 底层工具 |
| logger.py | 326 | 诊断日志 |
| translator.py | 447 | 翻译编排器 |
| agent_tools.py | 1155 | 工具集 + 上下文 |

总计约 2900 行。
