# 架构设计文档

## 总体架构

**Python 编排 + Agent 执行**：章节循环、内容切分、重试、写盘时机全部由 Python 控制，Agent 只在"翻译一个章节"这一粒度上自主调度工具。

早期版本是让 Agent 在单次 run 内翻完整本书，实际跑下来有两个致命问题：上下文随章节数无限累积，以及某一章失败后无法定位、无法单独重试。现在改成每章一次独立 run。

```
┌─────────────────────────────────────────────────────────┐
│                       CLI / API                          │
│                    (cli.py, client.py)                   │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│                   EpubTranslator                         │
│                (translator.py - 编排器)                  │
│                                                          │
│  - 读取 EPUB，回填缓存中已完成章节的译文                 │
│  - 逐章循环：切分 → 一次 Agent run → 校验落盘 → 重试     │
│  - 目录阶段、图片阶段各一次独立 run                      │
│  - 收尾写盘并如实报告完成/失败章节数                     │
└──────────────────────┬──────────────────────────────────┘
                       │ 每章一次 agent.run(deps=EpubContext)
                       ▼
┌─────────────────────────────────────────────────────────┐
│                 pydantic-ai Agent                        │
│                                                          │
│  - 循环取出分块、翻译、写回，直到本章分块取完            │
│  - 保存本章；被拒时按错误提示补译                        │
│  - 记录新出现的专有名词                                  │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              EPUB Toolsets (agent_tools.py)              │
│                                                          │
│  📖 信息查询:                                            │
│     - get_book_info / list_chapters                      │
│     - get_translation_progress                           │
│     - is_untranslated_buffer_empty                       │
│                                                          │
│  📝 章节翻译:                                            │
│     - get_untranslated_content   （取出一个分块）        │
│     - store_translation_chunk    （写入译文，可多次）    │
│     - save_translated_chapter    （保存，含完整性校验）  │
│                                                          │
│  📚 术语管理:                                            │
│     - get_glossary / update_glossary                     │
│                                                          │
│  📑 目录翻译:                                            │
│     - translate_toc / save_translated_toc                │
│                                                          │
│  🖼️ 图片翻译:                                            │
│     - list_images / get_image_base64                     │
│     - save_translated_image                              │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│                       支持模块                           │
│                                                          │
│  📦 EpubTools (epub_tools.py)                            │
│     - 章节提取、递归 HTML 分块、token 计数               │
│     - 导航文档（nav.xhtml）识别与标题替换                │
│                                                          │
│  📝 TranslationLogger (logger.py)                        │
│     - 分块尺寸、token 用量、工具调用序列、失败原因       │
│                                                          │
│  💾 CacheManager (cache_manager.py)                      │
│     - 进度 / 章节 / 图片缓存                             │
│                                                          │
│  📋 Models (models.py)                                   │
│     - Pydantic 数据模型                                  │
└─────────────────────────────────────────────────────────┘
```

`finalize_epub` 定义在 `agent_tools.py` 里，但**不是** Agent 工具，而是普通函数：写盘时机必须由 Python 在校验完成度之后决定，否则模型可能在漏章的情况下提前落盘。

## 核心组件

### 1. EpubTranslator（编排层）

**职责：**
- 读取 EPUB、加载缓存、把已完成章节的译文回填进 book 对象
- 计算待翻译章节列表，逐章调用 `_translate_chapter_with_retry`
- 每章开始前调用 `ctx.prepare_chapter(index)` 完成切分
- run 结束后**以缓存进度为准**校验本章是否真的落盘，失败则重试
- 目录 / 图片阶段各起一次独立 run
- 最后调用 `finalize_epub` 写盘，并如实报告 `completed/total`

**失败判定有两层：**
1. run 的输出里出现 `<tool_call` / `<function=` / `</function>` —— 模型把工具调用写成了纯文本，本轮实际什么都没保存
2. run 正常结束，但缓存进度里没有这一章

两者都触发重试，重试次数由 `MAX_CHAPTER_RETRIES` 控制。超过次数后该章记入 `failed_chapters`，输出文件里保持原文，不影响其余章节。

### 2. Agent（单章执行层）

每次 run 只处理一个章节，提示词里写明本章被切成了几块。Agent 的循环是：

```
get_untranslated_content(n)  → 翻译该块 → store_translation_chunk(n, 译文)
      ↑                                              │
      └──────────── 工具返回值告知还剩几块 ──────────┘
                              │ 取完
                              ▼
          [update_glossary] → save_translated_chapter(n)
```

`retries=3` 是 pydantic-ai 层的工具调用重试；`MAX_REQUESTS` 限制单次 run 的 API 请求数，防止模型陷入死循环。

### 3. Toolsets（工具层）

**设计原则：**
- 每个工具职责单一，工具间相互独立
- 通过 `RunContext[EpubContext]` 共享状态
- 工具的返回值同时承担"下一步该做什么"的引导作用，比如取分块时会附上"本章还剩 N 块"

**信息查询类：**
```python
get_book_info()                # 书籍元信息
list_chapters()                # 章节列表及翻译状态
is_untranslated_buffer_empty() # 本章分块是否已取完
get_glossary()                 # 术语表
get_translation_progress()     # 翻译进度
list_images()                  # 图片列表
```

**翻译操作类：**
```python
get_untranslated_content()  # 取出下一个待翻译分块（取出即出队）
store_translation_chunk()   # 追加写入译文，自动按顺序拼接
save_translated_chapter()   # 保存本章，含两道护栏
update_glossary()           # 更新术语表
translate_toc()             # 列出目录项
save_translated_toc()       # 保存目录，并同步 nav.xhtml
get_image_base64()          # 读取图片
save_translated_image()     # 保存图片
```

**Python 侧函数（不是工具）：**
```python
finalize_epub(ctx, output_path)  # 收尾写盘
```

### 4. EpubContext（上下文）

作为 Agent 的 `deps` 在所有工具间共享：

```python
class EpubContext:
    book: epub.EpubBook                     # EPUB 对象
    target_language: str                    # 目标语言
    source_language: str                    # 源语言
    cache_key: Optional[str]                # 缓存键
    cache_manager: Optional[CacheManager]   # 缓存管理器
    glossary: Dict[str, str]                # 术语表
    chapters: List                          # 正文章节列表
    images: List                            # 图片列表

    untranslated_buffer: Dict[int, List[str]]  # 各章待翻译分块队列
    translation_buffer: Dict[int, str]         # 各章译文缓冲区
    source_tags: Dict[int, int]                # 各章原文 HTML 标签数
    save_rejections: Dict[int, int]            # 各章 save 被拒次数
```

关键方法 `prepare_chapter(index)`：切分章节、重置该章所有中间状态、记录原文标签数，返回分块数。**它是普通方法，不是 Agent 工具**——见下节。

## 关键设计决策

### 切分必须由 Python 完成，不能暴露给模型

这是不变量。曾经把切分做成 Agent 工具，结果模型在翻译中途重新调用切分，把 `translation_buffer` 里已攒的译文清空了，该章永远保存不了。现在 `prepare_chapter` 在 run 之前把分块放进 `untranslated_buffer`，模型只能取、不能重置。

分块取出即出队，不可重新获取——所以 `save_translated_chapter` 被拒时，工具的错误提示会明确写"不要重新获取原文，请补译遗漏部分后追加写入"。

### 分块器必须能下钻单根元素

`EpubTools._atomize` 递归拆分：超过 `INPUT_MAX_TOKENS` 的元素先产出起始标签、递归处理内部、最后补结束标签。

早期版本只遍历 `body.children`，而 calibre 导出的 EPUB 常把整章包在一个 `<section>` 里 —— 于是"切分"后整章仍是一个 2.5 万 token 的块。这个块喂给模型后，输出被 `max_tokens` 截断 → 工具调用参数的 JSON 不完整 → 模型退化成把 `<tool_call>` 当普通文本吐出来。控制台上只显示一行"模型输出了文本形式的工具调用"，根因完全看不出来。

所有分块拼接后与原 body 内容完全一致，这是分块器的正确性约束。

### INPUT_MAX_TOKENS 必须显著小于 OUTPUT_MAX_TOKENS

译文（中文 token 密度更高）+ 完整 HTML 标签 + JSON 字符串转义，三者叠加后输出通常是输入的 1.5~2 倍。当前是 2500 / 8192。

### 完整性校验用标签数而非字符数

`save_translated_chapter` 的两道护栏：

1. **还有分块没取出来** → 直接拒绝，说明会漏内容
2. **译文标签数 / 原文标签数 < `MIN_TAG_RATIO`** → 判定漏译

第二道用标签数而不是字符数：中文译文字符数天然比英文原文少一半左右，按字符判断会大量误报；HTML 标签要求原样保留，标签数与语言无关。

首次不达标拒绝并要求补译，第二次仍不达标则**放行并告警** —— 避免模型和校验互相顶牛，卡死在重试循环里。被拒的译文会另存为 `*_chN_rejected.html` 供人工比对。

### 侧边栏目录（nav.xhtml）要单独同步

阅读器侧边栏的目录来自 EPUB3 导航文档，不来自 `book.toc`。而 ebooklib 的 `EpubWriter._write_items` 只对 `EpubNcx` / `EpubNav` 实例重新生成内容，其余原样回写。

calibre 导出的 EPUB 常常没在 OPF 里标 `properties="nav"`，读进来只是普通 `EpubHtml` —— 既不会被 ebooklib 重建，也会被 `_is_chapter` 当作目录页排除掉，两头落空，侧边栏永远是原文。

解决办法：`find_nav_documents` 按内容（`epub:type="toc"`）识别导航文档，`apply_nav_labels` 以 {原文标题: 译文标题} 替换 `<a>` 里的文本。page-list / landmarks 不翻译（前者是页码，后者是阅读器地标，翻了还会打乱条目对应）。

**操作导航文档必须用 `xml` 解析器。** `BeautifulSoup(..., "html.parser")` 会把 XHTML 里的 `<head>` 内容丢掉，写回去的 nav.xhtml 会缺 `<title>` 和样式表链接。lxml 本身已是 ebooklib 依赖。

### 目录译文必须缓存

`book.toc` 每次都从原始 EPUB 重新读出。续译时如果只看 `toc_translated` 这个布尔量就跳过翻译，产出的 toc.ncx 会退回原文。所以译后的标题列表存进 `TranslationProgress.toc_titles`，续译时由 `_restore_cached_toc` 回填 `book.toc` 并重新同步导航文档。

条目数对不上时（原书结构变了）不回填，直接重新翻译。

## 数据流

### 翻译流程

```
1. 用户调用 CLI
   ↓
2. create_translator() (client.py)
   ├─ create_epub_agent() → 注册 epub_toolset
   └─ new EpubTranslator(agent)
   ↓
3. translator.translate_epub()
   ├─ init_logger → .epub_translation_logs/{书名}_{时间戳}.log
   ├─ 读取 EPUB、检测源语言
   ├─ 加载缓存进度
   ├─ 创建 EpubContext
   └─ 回填已完成章节的译文（缓存内容缺失的章节会被踢回待翻译）
   ↓
4. 逐章循环（Python 控制，每章一次独立 run）
   ├─ ctx.prepare_chapter(index)        # 切分 + 记录原文标签数
   ├─ agent.run(单章提示词, deps=ctx)
   │    └─ get_untranslated_content → 翻译 → store_translation_chunk
   │       → [update_glossary] → save_translated_chapter
   └─ 校验缓存进度 → 未落盘则重试（最多 MAX_CHAPTER_RETRIES 次）
   ↓
5. 目录阶段（单次 run，可选）
   └─ translate_toc → save_translated_toc
      └─ 同时写回 book.toc（toc.ncx）与 nav.xhtml（侧边栏）
   ↓
6. 图片阶段（单次 run，默认关闭）
   ↓
7. finalize_epub(ctx, output)  # Python 收尾写盘
   ↓
8. 报告 completed/total，列出失败章节，返回输出文件路径
```

### 缓存机制

```
.epub_translation_cache/
├── {md5_hash}.json              # 翻译进度
│   ├─ source_lang / target_lang
│   ├─ total_chapters
│   ├─ completed_chapters []     # 已完成章节 ID
│   ├─ failed_chapters []
│   ├─ glossary {}               # 术语表
│   ├─ toc_translated            # 目录是否已翻译
│   ├─ toc_titles []             # 目录译文（按 book.toc 递归顺序）
│   └─ images_translated {}
│
└── {md5_hash}/
    ├── chapters/
    │   └── {chapter_id_hash}.html
    └── images/
        └── {image_name_hash}
```

缓存键为 `md5(文件绝对路径 + 目标语言)`。

**断点续传逻辑：**
1. 加载进度文件，取出 `completed_chapters`
2. 逐章从 `chapters/` 读回译文写进 book 对象
3. 进度说已完成但缓存文件丢失的章节，从 `completed_chapters` 中移除并重新翻译 —— 否则会静默输出原文
4. 只对不在 `completed_chapters` 里的章节起 run

## 诊断日志

翻译失败在控制台上往往只留一行错误。`logger.py` 把细节写入 `.epub_translation_logs/{书名}_{时间戳}.log`：

- 每章每次尝试的分隔行，逐块的 `tokens / chars / tags`
- 每次 run 的输出长度、输出片段、token 用量、实际发起的工具调用序列
- 文本形式工具调用的原始输出（用于判断是否为截断所致）
- 每次 save 被拒的原因；标签数不达标时把被拒译文另存为 `*_chN_rejected.html`
- `DATA` 前缀的 JSON 行（`save_chapter` / `chapter_failed` / `finish`），便于脚本统计失败分布

`get_logger()` 是模块级单例——`agent_tools.py` 里的工具函数拿不到 translator 实例，只能靠模块级变量共享。`DEBUG_MODE` 只控制控制台摘要，文件始终记录完整信息。

## 未来扩展方向

### 1. 保留章节外壳

当前 `save_translated_chapter` 写入的是 body 级分块的拼接结果，`<html>` / `<head>` 外壳及其中的 CSS 链接会丢失。修复方向是保存时用原章节的 soup 做模板、只替换 body 内容。

### 2. 并发翻译

`concurrent_manager.py` 已提供 asyncio 并发控制和速率限制，但当前流程是串行的。章节之间除术语表外没有强依赖，接入并发的主要顾虑是术语表的写竞争。

### 3. 添加新工具

```python
@epub_toolset.tool
def translate_metadata(ctx: RunContext[EpubContext]) -> str:
    """翻译书籍元数据（标题、作者等）"""
    ...

@epub_toolset.tool
def extract_footnotes(ctx: RunContext[EpubContext]) -> str:
    """提取所有脚注"""
    ...
```

### 4. 跨书籍共享术语表

当前术语表按书缓存。系列作品可以考虑全局术语表预加载。

## 总结

| 层 | 负责 |
|----|------|
| **EpubTranslator** | 章节循环、切分时机、重试、写盘时机 |
| **Agent** | 单章内的翻译与工具调度 |
| **Toolsets** | 原子化的 EPUB 操作 + 保存护栏 |
| **EpubContext** | 跨工具的共享状态 |
| **CacheManager** | 进度持久化与断点续传 |
| **TranslationLogger** | 失败可回溯 |

划界的原则是：**凡是"错了会导致整章白翻"的决策，都放在 Python 侧**（切分、写盘、完整性判定、重试）；模型只负责它真正擅长的部分——翻译文本。
