# 架构设计文档

本文件是本项目**唯一**的架构与设计决策记录：架构分层、数据流、关键设计决策与不变量、诊断日志，全部写在这里。CLAUDE.md 只保留命令和红线索引，新增的设计结论请追加到本文件对应小节。

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
                       │ 外层套 agent.sequential_tool_calls()
                       ▼
┌─────────────────────────────────────────────────────────┐
│                 pydantic-ai Agent                        │
│                                                          │
│  - 循环发放分块、翻译、写回，直到本章每块都有译文        │
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
│     - check_chapter_progress   （本章还缺哪些块）        │
│     - get_translation_progress                           │
│                                                          │
│  📝 章节翻译:                                            │
│     - get_untranslated_content   （发放缺译文的块）      │
│     - store_translation_chunk    （按块号写入，可多次）  │
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
│     - 进度 / 章节 / 图片缓存，加锁 + 原子写入            │
│                                                          │
│  📋 Models (models.py)                                   │
│     - Pydantic 数据模型                                  │
└─────────────────────────────────────────────────────────┘
```

`finalize_epub` 定义在 `agent_tools.py` 里，但**不是** Agent 工具，而是普通函数：写盘时机必须由 Python 在校验完成度之后决定，否则模型可能在漏章的情况下提前落盘。

**技术栈**：Python >= 3.10、pydantic-ai（Agent 框架）、ebooklib（EPUB 解析）、BeautifulSoup4 + lxml（HTML/XML 操作）、typer（CLI）、tiktoken（token 计数）、ruff、uv。

## 核心组件

### 1. EpubTranslator（编排层）

**职责：**
- 读取 EPUB、加载缓存、把已完成章节的译文回填进 book 对象
- 计算待翻译章节列表，逐章调用 `_translate_chapter_with_retry`
- 每章每次尝试前调用 `ctx.prepare_chapter(index)` 完成切分并重置该章状态
- run 结束后**以缓存进度为准**校验本章是否真的落盘，失败则重试
- 目录 / 图片阶段各起一次独立 run
- 最后调用 `finalize_epub` 写盘，并如实报告 `completed/total`

三个 run 入口（章节、目录、图片）统一走 `_run_agent`，由它套上 `sequential_tool_calls()` 与 `UsageLimits(request_limit=MAX_REQUESTS)`。

**失败判定有两层：**
1. run 的输出里出现 `<tool_call` / `<function=` / `</function>` —— 模型把工具调用写成了纯文本，本轮实际什么都没保存
2. run 正常结束，但缓存进度里没有这一章（含"保存了但判定不完整"）

两者都触发重试，尝试次数为 `MAX_CHAPTER_RETRIES + 1`。超过次数后该章记入 `failed_chapters`，输出文件里保持原文或残缺译文，不影响其余章节。

### 2. Agent（单章执行层）

每次 run 只处理一个章节，提示词里写明本章被切成了几块、合法块号范围。Agent 的循环是：

```
get_untranslated_content(n)  → 翻译该块 → store_translation_chunk(n, chunk_index, 译文)
      ↑                                              │
      └──────────── 工具返回值告知还缺哪些块 ────────┘
                              │ 每块都有译文
                              ▼
          [update_glossary] → save_translated_chapter(n)
```

`retries=3`（client.py）同时是 pydantic-ai 的 `_max_tool_retries` 和 `_max_result_retries`；`MAX_REQUESTS` 限制单次 run 的 API 请求数，是模型陷入死循环时唯一的刹车（原因见"工具错误不计重试"）。

### 3. Toolsets（工具层）

**设计原则：**
- 每个工具职责单一，工具间相互独立
- 通过 `RunContext[EpubContext]` 共享状态
- 工具的返回值同时承担"下一步该做什么"的引导作用，比如发放分块时会附上"本章还缺 N 块、块号是哪些"

**信息查询类：**
```python
get_book_info()                # 书籍元信息
list_chapters()                # 章节列表及翻译状态
check_chapter_progress()       # 本章还缺哪些块（块号）
get_glossary()                 # 术语表
get_translation_progress()     # 翻译进度
list_images()                  # 图片列表
```

**翻译操作类：**
```python
get_untranslated_content()  # 发放下一个还没有译文的块（非破坏性，不弹出）
store_translation_chunk()   # 按 chunk_index 写入译文，同块多次调用则追加
save_translated_chapter()   # 保存本章，含护栏
update_glossary()           # 更新术语表
translate_toc()             # 列出目录项
save_translated_toc()       # 保存目录，并同步 nav.xhtml
get_image_base64()          # 读取图片
save_translated_image()     # 保存图片
```

**Python 侧函数（不是工具）：**
```python
finalize_epub(ctx, output_path)   # 收尾写盘
collect_toc_titles / apply_toc_titles / sync_nav_documents   # 目录与导航同步辅助
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

    chapter_chunks: Dict[int, List[str]]           # 各章原文分块（翻译期间只读）
    chunk_translations: Dict[int, Dict[int, str]]  # {章节: {块号: 译文}}
    chunk_warned: Dict[int, Set[int]]              # 已提醒过"疑似漏译"的块
    saved_chapters: Set[int]                       # 完整保存成功的章节
    incomplete_chapters: Dict[int, str]            # 保存了但判定不完整 {章节: 原因}
```

派生状态一律由方法实时计算，不额外维护副本：`chunk_count` / `pending_chunks`（缺译文的块号）/ `assembled_translation`（按块号排序拼接）/ `source_tag_count` / `thin_chunks`（译文偏短的块号）。

关键方法 `prepare_chapter(index)`：切分章节、重置该章所有中间状态、记录分块尺寸到日志，返回分块数。**它是普通方法，不是 Agent 工具**——见下节。

## 关键设计决策与不变量

标【不变量】的条目改动会导致整章甚至整本书白翻，修改前务必读完对应的证据段。

### 分块与译文流转

**【不变量】切分必须由 Python 完成，不能暴露给模型。**
`EpubContext.prepare_chapter` 在 run 之前把章节切好放进 `chapter_chunks`，模型只能取、不能重置。曾经把切分做成 Agent 工具，模型在翻译中途重新切分会清空已攒的译文，导致该章永远保存不了。

**【不变量】分块发放必须是非破坏性的。**
`get_untranslated_content` 只"发放下一个还没有译文的块"，不弹出；`store_translation_chunk` 必须带 `chunk_index`，写入哪一块由块号决定，译文按块号排序拼接（模型乱序写入也能还原）。
早期版本用 `pending.pop(0)` 队列，一旦某块的 store 调用被输出截断，这块原文就永久消失了——模型只能跳过它继续下一块（静默漏译），或者整章重来。日志实测一次运行里 3 个章节各丢 1 块，而全章标签比例仍在 86% 以上，`MIN_TAG_RATIO` 结构上抓不住这种 1/10 的丢失。

**分块器必须能下钻单根元素。**
`EpubTools._atomize` 递归拆分：超过 `INPUT_MAX_TOKENS` 的元素先产出起始标签、递归处理内部、再补结束标签。
早期版本只遍历 `body.children`，而 calibre 导出的 EPUB 常把整章包在一个 `<section>` 里——于是"切分"后整章仍是一个 2.5 万 token 的块。这个块喂给模型后，输出被 `max_tokens` 截断 → 工具调用参数的 JSON 不完整 → 模型退化成把 `<tool_call>` 当普通文本吐出来，控制台上只显示一行"模型输出了文本形式的工具调用"，根因完全看不出来。
所有分块拼接后与原 body 内容完全一致，这是分块器的正确性约束。

**`INPUT_MAX_TOKENS` 必须显著小于 `OUTPUT_MAX_TOKENS`。当前取值 `5000 / 16384`。**
译文 + 完整 HTML 标签 + JSON 字符串转义叠加后输出会放大：拿现有缓存和日志里配对的 500 组"发放/写入"实测，输出/输入 token 比中位 1.32、p90 1.68、p99 1.87、最大 1.99（其中 JSON 转义只占 +1%，主要来自中文 token 密度）。此外推理 token 也计入 `max_tokens` 却不出现在 `result.output` 里，因此"输出看着不长"并不代表没被截断。
按最坏比例 2.0 算：`5000 × 2 + 6000(推理) ≈ 16000 < 16384`，即使供应商不认 `reasoning_effort=low`、推理照旧吃掉 6000 token 也留有余量。这里的 6000 是留给推理的预留额度，不是观测值——stepfun 至今没在 `usage.details` 里报过 `reasoning_tokens`（现有日志里 details 只出现过 `cached_tokens`），推理到底吃了多少无法从日志验证，能直接观测的只有 `finish_reason`。
调大 `INPUT_MAX_TOKENS` 能成倍减少分块数，进而按平方级降低单章累计输入 token（每次请求都要重发已累积的对话），但单请求峰值上下文不变；代价是一旦某块译文超预算被截断，重试同一块还会再次超出，整章会耗尽重试次数。

### 并发与持久化

**【不变量】同一响应里的多个工具会被并发执行，必须挡住。**
pydantic-ai 在 `_agent_graph.py` 里对一个响应内的多个 tool call 走 `asyncio.create_task` 并发执行（`should_call_sequentially` 为假时），而本项目的工具全是同步函数——会被丢进线程池真正并行。所有工具共享同一个 `EpubContext` 和同一个进度文件，并行就会互相覆盖。因此 `EpubTranslator._run_agent` 用 `agent.sequential_tool_calls()` 包住每一次 run。
实测：一个响应同时发 `update_glossary` + `save_translated_chapter` 时，两个 `load→改→save` 交错，术语表更新被整体丢弃，且短文档只覆盖了进度文件前 339 字节、尾部残留上一版内容，之后 `load_progress` 一直报 `Extra data: line 14 column 2` → `_is_chapter_done` 恒为假 → 每章耗尽重试、全书判定失败。

**【不变量】进度文件的写入必须原子，"读—改—写"必须在锁内。**
`CacheManager` 用 `threading.RLock` 串行化进度读写，`_save_locked` 先写 `{key}.json.{pid}.tmp` 再 `os.replace`（`write_text` 会先截断，交错写入就会撕裂文件）。跨调用的改动走 `update_progress(cache_key, mutate)`，把改动塞进同一个临界区；单独 `load_progress` → 改 → `save_progress` 的写法会丢更新，已从代码里清除。
`load_progress` 另外容忍历史遗留的尾部残留：用 `raw_decode` 取首个完整文档并立刻重写成干净文件。

**`_is_chapter_done` 读不到进度时退回 `ctx.saved_chapters`。**
进度文件损坏或被删时若一律返回假，每章都会被判成"未落盘"而白白耗尽重试次数。

**进度 JSON 第一个键是 `book_name`。**
缓存键是"文件绝对路径 + 目标语言"的 MD5，光看文件名认不出是哪本书，所以把书名（EPUB 文件名去后缀，与日志文件同名）记在最前面。旧缓存缺这个字段时默认空串，并在下次运行时回填并立即落盘——整本已翻完时后面不会再有 `save_progress` 把它写出去。

### 完整性判定

**完整性校验用标签数而非字符数（`MIN_TAG_RATIO = 0.8`）。**
中文译文字符数天然比英文原文少一半左右，按字符判断会大量误报；HTML 标签要求原样保留，标签数与语言无关。

**块内漏译在 store 时刻校验，块缺失在 save 时刻硬拦。**
store 时比对该块译文与原文的标签数，不达标当场要求补译（同一块只提醒一次，避免模型卡在补译-拒绝的死循环里）——此时模型手里还有这块原文，能直接补；等到 save 才发现，模型已经不知道漏的是哪一段了。
save 侧"每块都必须有译文"是硬规则、不设放行次数：分块可以反复获取，模型总能补上，放行等于把漏译静默写进成品。

**【不变量】判定不完整的章节不写进 `completed_chapters`。**
全章标签数仍不达标时，译文照样写进 book（部分译文比整章原文有用）并记入 `ctx.incomplete_chapters`，但不标记完成，从而触发本次重试、下次 `--resume` 重译。曾经"拒绝一次就放行并标记完成"，残缺章节会被 `--resume` 永远跳过。

**工具的错误不计入重试，只有 `request_limit` 兜底。**
工具的错误是 `return "错误：…"` 而不是 `raise ModelRetry`，pydantic-ai 视为调用成功，既不计入 `max_tool_retries` 也不中断 run。因此模型可以在同一个错误上无限循环，唯一的刹车是 `UsageLimits(request_limit=MAX_REQUESTS)`。这条约束直接推导出诊断日志里的"每次工具调用都留一行"不变量。

### 目录与导航

**侧边栏目录（nav.xhtml）要单独同步。**
阅读器侧边栏的目录来自 EPUB3 导航文档，不来自 `book.toc`。而 ebooklib 的 `EpubWriter._write_items` 只对 `EpubNcx` / `EpubNav` 实例重新生成内容，其余原样回写。calibre 导出的 EPUB 常常没在 OPF 里标 `properties="nav"`，读进来只是普通 `EpubHtml`——既不会被 ebooklib 重建，也会被 `_is_chapter` 当作目录页排除掉，两头落空，侧边栏永远是原文。
解决办法：`find_nav_documents` 按内容（`epub:type="toc"`）识别导航文档，`apply_nav_labels` 以 {原文标题: 译文标题} 替换 `<a>` 里的文本。page-list / landmarks 不翻译（前者是页码，后者是阅读器地标，翻了还会打乱条目对应）。

**操作导航文档必须用 `xml` 解析器。**
`BeautifulSoup(..., "html.parser")` 会把 XHTML 里的 `<head>` 内容丢掉，写回去的 nav.xhtml 会缺 `<title>` 和样式表链接。lxml 本身已是 ebooklib 依赖。

**目录译文必须缓存到 `TranslationProgress.toc_titles`。**
`book.toc` 每次都从原始 EPUB 重新读出。续译时如果只看 `toc_translated` 这个布尔量就跳过翻译，产出的 toc.ncx 会退回原文。所以译后的标题列表存进缓存，续译时由 `_restore_cached_toc` 回填 `book.toc` 并重新同步导航文档；条目数对不上时（原书结构变了）不回填，直接重新翻译。
`collect_toc_titles` 与 `apply_toc_titles` 必须严格同序——一个负责取、一个负责放，顺序不一致会让译文错位到别的条目上。

### 写盘与配置

**写盘由 Python 收尾。**
`finalize_epub` 是普通函数而非 Agent 工具，避免模型中途或漏章时提前落盘。

**settings.py 同时承载配置和提示词。**
系统提示词放在 settings.py 而非单独文件，便于直接修改翻译规则和风格。其中的 `{target_language}` 在 client.py 中通过 `str.format()` 注入。

**模型侧参数走 `OpenAIChatModelSettings`。**
`openai_reasoning_effort` 会被 pydantic-ai 直通成请求里的 `reasoning_effort`（`models/openai.py` 无模型名门槛），用它把推理强度压到 `low`，给译文腾出 `max_tokens` 预算。
**输出上限同时发两遍**：`max_tokens=OUTPUT_MAX_TOKENS` 被 pydantic-ai 发成 `max_completion_tokens`，而 stepfun 这类只认 `max_tokens` 的供应商靠 `extra_body={"max_tokens": OUTPUT_MAX_TOKENS}` 兜住（`extra_body` 在 `models/openai.py:674` 直接合并进请求体）。两个字段值相同、谁认哪个都生效，避免供应商命名差异把上限静默丢掉。

**关于 `finish_reason=length` 的正确读法**（推翻了早期结论）：早期文档写"日志里仍出现 `length` 就说明两个字段都没被认"，这是反的——`length` 恰恰是**有上限在生效**的证据，只是无法从 finish_reason 区分截断发生在我们发的 16384 还是供应商自己的默认值。
现有日志里 `length` 一共出现过 1 次（《The Design of Everyday Things》第 10 章，15 次响应中的第 2 次）：那次响应没能发出任何工具调用，pydantic-ai 补一轮请求后自行接上，本章 5 块全部写入、第 1 次尝试就保存成功（`tags 1142/1252`、`thin_chunks []`）。
**结论：单次 `length` 是可恢复的，真正致命的是截断落在 `store_translation_chunk` 的参数中途**——参数 JSON 不完整，模型就会退化成把 `<tool_call>` 当文本输出（另一份日志里连续发生过 6 次）。所以看到 `length` 先看它有没有伴随"文本形式的工具调用"，偶发一次不必调参。

**deepseek 兼容。**
client.py 中显式设置 `extra_body={"thinking": {"type": "disabled"}}`，兼容 deepseek 等需要禁用思考模式的模型。

**concurrent_manager.py 当前未使用。**
该模块提供 asyncio 并发控制和速率限制能力，但当前翻译流程是串行的，如果需要并行翻译多本书可以引入。

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
   ├─ 加载缓存进度（回填 book_name）
   ├─ 创建 EpubContext
   └─ 回填已完成章节的译文（缓存内容缺失的章节会被踢回待翻译）
   ↓
4. 逐章循环（Python 控制，每章一次独立 run）
   ├─ ctx.prepare_chapter(index)        # 切分 + 重置该章状态 + 记录分块尺寸
   ├─ agent.run(单章提示词, deps=ctx)   # 外层 sequential_tool_calls + request_limit
   │    └─ get_untranslated_content → 翻译 → store_translation_chunk(块号)
   │       → [update_glossary] → save_translated_chapter
   └─ 校验缓存进度 → 未落盘/判定不完整则重试（共 MAX_CHAPTER_RETRIES + 1 次尝试）
   ↓
5. 目录阶段（单次 run，可选）
   └─ translate_toc → save_translated_toc
      └─ 同时写回 book.toc（toc.ncx）与 nav.xhtml（侧边栏）
   ↓
6. 图片阶段（单次 run，默认关闭）
   └─ list_images → get_image_base64 → save_translated_image
   ↓
7. finalize_epub(ctx, output)  # Python 收尾写盘
   ↓
8. 报告 completed/total，列出失败章节，返回输出文件路径
```

### 缓存机制

```
.epub_translation_cache/
├── {md5_hash}.json              # 翻译进度
│   ├─ book_name                 # 书名（第一个键，用于认出这是哪本书）
│   ├─ source_lang / target_lang
│   ├─ total_chapters
│   ├─ completed_chapters []     # 已完成章节 ID（只有完整保存才写进来）
│   ├─ failed_chapters []
│   ├─ glossary {}               # 术语表
│   ├─ toc_translated            # 目录是否已翻译
│   ├─ toc_titles []             # 目录译文（按 book.toc 递归顺序）
│   └─ images_translated {}
│
└── {md5_hash}/
    ├── chapters/
    │   └── {md5(chapter_id)}.html
    └── images/
        └── {md5(image_name)}
```

缓存键为 `md5(文件绝对路径 + 目标语言)`。

**断点续传逻辑：**
1. 加载进度文件，取出 `completed_chapters`
2. 逐章从 `chapters/` 读回译文写进 book 对象
3. 进度说已完成但缓存文件丢失的章节，从 `completed_chapters` 中移除并重新翻译——否则会静默输出原文
4. 只对不在 `completed_chapters` 里的章节起 run

## 诊断日志

翻译失败在控制台上往往只留一行错误。`logger.py` 把细节写入 `.epub_translation_logs/{书名}_{时间戳}.log`：

- 每章每次尝试的分隔行、逐块的 `tokens / chars / tags`
- 每次 run 的输出长度、输出片段、token 用量、实际发起的工具调用序列
- token 用量里带 `usage.details`：`reasoning_tokens` 计入 `max_tokens` 却不出现在 `result.output` 里，是"输出看着不长却被截断"的隐形消耗者，所以只要供应商报了就记下来（stepfun 至今只报 `cached_tokens`，没报过 `reasoning_tokens`）
- 每次模型响应的 `finish_reason` 序列（归一化值 + 括号内供应商原值）。`length` 是输出被 `max_tokens` 截断的直接证据，出现时额外打一条 WARN 并提示控制台；读法见"关于 `finish_reason=length` 的正确读法"
- 每次 save 被拒的原因、判定不完整的章节和块号
- **【不变量】每一次工具调用都留一行。** 本身有专门日志的工具（发放块 / 写入块 / 保存章节 / 保存目录）保持原样，其余工具走 `logger.tool_call`（INFO），所有错误与空转 return 走 `logger.tool_error`（WARN + VERBOSE 控制台）。
  原因见"工具的错误不计入重试"：这类错误天然可以无限循环。实测一章 208 token 的 titlepage 空转掉 128 个请求、4 分 01 秒后抛 `UsageLimitExceeded`，而日志里只有一行"发放块 0"，事后完全无法判断它在调什么（`run_result` 只在 run 成功返回时才写，异常路径什么都没有）。空转时刷屏的重复行正是需要的证据，且被 `request_limit` 天然限量。
- 诊断内容按 `{日志名}_ch{章}_try{尝试}_{类型}.{后缀}` 完整落盘（不截断）：`leaked.txt` 是文本形式工具调用的原始输出（据此判断是否截断，也保住了里面已译好的正文），`rejected.html` 是判定漏译的全章译文
- `DATA` 前缀的 JSON 行（`save_chapter` / `chapter_failed` / `finish`，含 `thin_chunks` 块号），便于脚本统计失败分布

`get_logger()` 是模块级单例——`agent_tools.py` 里的工具函数拿不到 translator 实例，只能靠模块级变量共享。

控制台输出详细程度由 `ConsoleLevel` 分级控制，**文件日志不受等级影响，始终记录完整信息**：

| 等级 | 内容 | 入口 |
|------|------|------|
| `QUIET` | 只有错误 | CLI `-q` |
| `NORMAL` | 进度摘要（章节进度、缓存恢复等） | — |
| `VERBOSE` | + 分块尺寸、token 用量、被拒/空转提示 | 模块级默认 |
| `DEBUG` | + 工具调用序列、输出片段、落盘文件路径 | CLI `-v` |

编程式调用用 `set_console_level()` 或 `translate_epub(console_level=...)` 注入；错误一律打到 stderr 且不受等级限制。

## 已知未解决问题

**章节样式丢失。** `save_translated_chapter` 写入的是 body 级分块的拼接结果，`<html>` / `<head>` 外壳及其中的 CSS 链接会丢失。修复方向是保存时用原章节的 soup 做模板、只替换 body 内容。**用户已明确表示暂缓处理。**

**无测试。** 当前没有单元测试或集成测试，所有结论靠真实翻译跑出来的日志验证。

## 未来扩展方向

### 1. 让工具错误进入重试计数

把明显写错的参数（块号越界、章节越界）改成 `raise ModelRetry`，`max_tool_retries=3` 才会真正接管，不必等 `request_limit` 烧满。代价是要区分"模型写错"和"状态本就如此"，后者不该消耗重试。

### 2. 并发翻译

`concurrent_manager.py` 已提供 asyncio 并发控制和速率限制，但当前流程是串行的。章节之间除术语表外没有强依赖，接入并发的主要顾虑是术语表和进度文件的写竞争——后者已经在锁内原子写入，前者需要合并策略。

### 3. 块级续译

目前缓存的最小单位是章节，一章没保存成功则整章重翻。把 `chunk_translations` 也落盘可以让重试只补缺失的块。

### 4. 添加新工具

```python
@epub_toolset.tool
def translate_metadata(ctx: RunContext[EpubContext]) -> str:
    """翻译书籍元数据（标题、作者等）"""
    ...
```

### 5. 跨书籍共享术语表

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
