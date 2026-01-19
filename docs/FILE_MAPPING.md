# 文件对照表

## 核心文件清单

| 文件名 | 说明 | 主要内容 |
|--------|------|----------|
| **auto_epub/models.py** | 数据模型 | TranslationResult, ChapterTranslation, ImageTranslationResult, TranslationProgress |
| **auto_epub/epub_tools.py** | EPUB 基础工具类 | EpubTools 静态类：get_default_language, get_all_chapters, count_tokens, split_html_content 等 |
| **auto_epub/cache_manager.py** | 缓存管理器 | CacheManager 类：save_progress, load_progress, save_chapter, load_chapter 等 |
| **auto_epub/tools.py** | Agent 工具集 | epub_toolset, EpubContext, 15+ 工具函数（get_book_info, save_translated_chapter 等）|
| **auto_epub/translator.py** | 翻译器协调器 | EpubTranslator 类：translate_epub 主流程，构建任务提示词 |
| **auto_epub/client.py** | Agent 客户端 | create_epub_agent, create_translator |
| **auto_epub/cli.py** | 命令行接口 | Typer app, translate 命令, clear_cache 命令 |
| **auto_epub/config.py** | 配置加载 | 从 .env 加载 API 配置 |
| **auto_epub/settings.py** | 常量配置 | AGENT_SYSTEM_PROMPT, TIMEOUT, TEMPERATURE 等 |
| **auto_epub/__init__.py** | 包初始化 | 导出主要类和函数 |

## 配置和文档

| 文件名 | 说明 |
|--------|------|
| **main.py** | 主入口文件 |
| **example.py** | 使用示例代码 |
| **.env.example** | 环境变量模板 |
| **requirements.txt** | Python 依赖 |
| **pyproject.toml** | 项目配置 |
| **README.md** | 完整使用文档 |
| **QUICKSTART.md** | 快速开始指南 |
| **ARCHITECTURE.md** | 架构设计文档 |

## 重要区分

### epub_tools.py vs agent_tools.py

**epub_tools.py (基础工具类)**
- 静态工具类 `EpubTools`
- 提供 EPUB 文件操作的底层方法
- 不依赖 pydantic-ai
- 可以独立使用

**agent_tools.py (Agent 工具集)**
- 定义 `epub_toolset` (FunctionToolset)
- 定义 `EpubContext` (Agent deps)
- 15+ 工具函数，供 Agent 调用
- 依赖 pydantic-ai
- 使用 @epub_toolset.tool 装饰器

### 使用关系

```
Agent
  ↓ 使用
tools.py (epub_toolset)
  ↓ 调用
epub_tools.py (EpubTools)
  ↓ 操作
EPUB 文件
```

## 调用链示例

### 翻译一个章节

```
CLI (cli.py)
  ↓
create_translator() (client.py)
  ↓
translator.translate_epub() (translator.py)
  ↓
agent.run(task_prompt, deps=EpubContext) (Agent)
  ↓
Agent 决定调用 get_chapter_content (tools.py)
  ↓
EpubTools.get_all_chapters() (epub_tools.py)
  ↓
ebooklib.epub 库
```

### 保存翻译结果

```
Agent 完成翻译
  ↓
Agent 调用 save_translated_chapter (tools.py)
  ↓
cache_manager.save_chapter() (cache_manager.py)
  ↓
写入文件系统
```

## 目录结构

```
epub-translator/
├── auto_epub/
│   ├── __init__.py           ← 包初始化
│   ├── models.py             ← Pydantic 数据模型
│   ├── epub_tools.py         ← EPUB 基础工具（静态类）
│   ├── cache_manager.py      ← 缓存管理
│   ├── tools.py              ← Agent 工具集（Toolsets）
│   ├── translator.py         ← 翻译器协调器
│   ├── client.py             ← Agent 客户端
│   ├── cli.py                ← 命令行接口
│   ├── config.py             ← 配置加载
│   └── settings.py           ← 常量配置
├── main.py                   ← 主入口
├── example.py                ← 使用示例
├── .env.example              ← 环境变量模板
├── .env                      ← API 配置（需创建）
├── requirements.txt          ← 依赖列表
├── pyproject.toml            ← 项目配置
├── README.md                 ← 完整文档
├── QUICKSTART.md             ← 快速开始
└── ARCHITECTURE.md           ← 架构说明
```

## 文件大小估计

| 文件 | 行数 | 说明 |
|------|------|------|
| models.py | ~50 | 4个数据模型 |
| epub_tools.py | ~100 | 工具类方法 |
| cache_manager.py | ~100 | 缓存管理 |
| agent_tools.py | ~400 | 15+ 工具函数 |
| translator.py | ~200 | 翻译协调逻辑 |
| client.py | ~60 | Agent 创建 |
| cli.py | ~100 | 命令行接口 |
| config.py | ~30 | 配置加载 |
| settings.py | ~80 | 常量和提示词 |

总计核心代码约 1100+ 行。
