# 架构设计文档

本文件是本项目**唯一**的架构与设计决策记录：架构分层、数据流、关键设计决策与不变量、诊断日志，全部写在这里。CLAUDE.md 只保留命令和红线索引，新增的设计结论请追加到本文件对应小节。

## 总体架构

**Python 编排 + Agent 执行**：章节循环、内容切分、块级循环、重试、写盘时机全部由 Python 控制。**章节正文的翻译是"一块原文进、一块译文出"的纯函数调用，模型没有任何工具可调**；工具只剩目录与图片两个阶段在用。

演进路线是一路收窄模型的自主权：最早让 Agent 在单次 run 内翻完整本书（上下文随章节数无限累积，且某章失败后无法定位）→ 改成每章一次独立 run、模型在 run 内用工具循环取块存块（单章内 message history 仍然无界累积，37 块的章节必然撞 400，见"块级 run 的由来"）→ 现在每块一次独立 run，单请求输入与块序号无关。

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
│  - 逐章循环：切分 → 逐块翻译 → 校验落盘 → 重试           │
│  - 目录阶段、图片阶段各一次独立 run                      │
│  - 收尾写盘并如实报告完成/失败章节数                     │
└─────────┬───────────────────────────────┬───────────────┘
          │ 每块一次 chunk_agent.run()    │ 目录 / 图片各一次
          │ 无 deps、无工具、无 history   │ agent.run(deps=EpubContext)
          │                               │ 外层套 sequential_tool_calls()
          ▼                               ▼
┌──────────────────────────┐  ┌──────────────────────────────┐
│  chunk_agent（无工具）   │  │   epub_agent（带工具集）     │
│  chunk_translator.py     │  │                              │
│                          │  │  - 目录：列出→翻译→保存      │
│  一块原文 → 一块译文     │  │  - 图片：读取→识别→写回      │
│  上下文靠"接力包"拼进    │  │  - 顺手记录专有名词          │
│  prompt，不靠 history    │  │                              │
└──────────────────────────┘  └──────────────┬───────────────┘
                                             │
                       ┌─────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────┐
│              EPUB Toolsets (agent_tools.py)              │
│              **只服务目录与图片两个阶段**                │
│                                                          │
│  📖 信息查询:                                            │
│     - get_book_info / list_chapters                      │
│     - get_translation_progress                           │
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
│     - 分块尺寸、token 用量、每块每次尝试一行、失败原因   │
│                                                          │
│  💾 CacheManager (cache_manager.py)                      │
│     - 进度 / 章节 / **块** / 图片缓存，加锁 + 原子写入   │
│                                                          │
│  📋 Models (models.py)                                   │
│     - Pydantic 数据模型                                  │
└─────────────────────────────────────────────────────────┘
```

`finalize_epub` 与 `finalize_chapter` 都定义在 `agent_tools.py` 里，但**不是** Agent 工具，而是普通函数：写盘时机必须由 Python 在校验完成度之后决定，否则模型可能在漏章的情况下提前落盘。

**技术栈**：Python >= 3.10、pydantic-ai（Agent 框架）、ebooklib（EPUB 解析）、BeautifulSoup4 + lxml（HTML/XML 操作）、typer（CLI）、tiktoken（token 计数）、ruff、uv。

## 核心组件

### 1. EpubTranslator（编排层）

**职责：**
- 读取 EPUB、加载缓存、把已完成章节的译文回填进 book 对象
- 计算待翻译章节列表，逐章调用 `_translate_chapter_with_retry`
- **每章只切分一次**（`ctx.prepare_chapter(index)`），章级重试不重切
- 逐块调用 `translate_chapter_chunks`，再用 `finalize_chapter` 校验并落盘
- 目录 / 图片阶段各起一次独立 run
- 最后调用 `finalize_epub` 写盘，并如实报告 `completed/total`

两个工具型 run 入口（目录、图片）统一走 `_run_agent`，由它套上 `sequential_tool_calls()` 与 `UsageLimits(request_limit=MAX_REQUESTS)`，并记录用量与结束原因。块级 run 不走这里：它没有工具，不需要串行化守卫，用量由 `chunk_result` 逐块记录。

**失败判定分两级：**

*块级*（`validate_chunk`，每块一次）：译文为空、混进 `<tool_call` / `<function=` / `</function>`、`finish_reason=length`、或块级标签比例低于 `MIN_BLOCK_TAG_RATIO`。每块最多 `MAX_CHUNK_RETRIES + 1` 次尝试。

*章级*（`finalize_chapter`）：还有块完全没有译文，或全章块级标签比例不达标。

章级重试的额度是 `MAX_CHAPTER_RETRIES + 1`，但**它基本不会真的重跑**：`chunk_attempts` 跨章级重试累计，而"每块都达标 ⇒ 加总必然达标"意味着整章不达标必然有块没过；那些块的块级额度既然已经在本轮用尽，再来一轮只会原地空转。所以 `_translate_chapter_with_retry` 在"所有坏块额度都用尽"时直接收工，不做无意义的循环。留着这层循环是为了兜住"坏块还有额度、却因为异常提前退出块循环"的情形。

超过次数后该章记入 `failed_chapters`，输出文件里保持原文或残缺译文，不影响其余章节。

### 2. 块级翻译（chunk_translator.py）

每块一次独立 `agent.run`（`settings.STREAMING=False` 默认走非流式；置 True 时改用 `run_stream` 流式收全文，超时按「等响应头 / 相邻 delta 沉默」计），**run 与 run 之间没有 message history**，所以单请求输入与块序号无关：

```
Python: 取第 i 块原文 + 拼接接力包
   → chunk_agent.run(prompt)         # 无工具、无 deps、无历史；非流式收全文再校验
   → clean_model_html（剥围栏/前言）
   → split_terms_block（摘掉末尾术语块）
   → validate_chunk（块级标签比例 / 截断 / 工具调用泄漏）
   → 通过：写 chunk_translations[章][i] + 写块缓存 + 并入术语表
     不通过：留在内存但不进缓存，重试同一块
```

上下文靠 `build_carryover` 拼进 prompt，硬上限 `CARRYOVER_MAX_TOKENS`，超了按「接缝 > 术语 > 风格锚点」的优先级砍：

| 段 | 内容 | 条件 |
|---|---|---|
| 接缝 | 上一块原文尾 + 译文尾各 `CARRYOVER_SEAM_CHARS` 字 | `chunk_index > 0` |
| 术语 | `glossary` 里 key 真的出现在**当前块原文**中的条目 | 有命中 |
| 风格锚点 | 本章首块原文/译文各 `CARRYOVER_STYLE_CHARS` 字 | `chunk_index >= 2` |

**这个包必须是常量级。** 一旦让它随块号增长（例如带上"本章已译全文"），就又回到了被 400 撞墙的老路。

### 3. Toolsets（工具层）

**设计原则：**
- 每个工具职责单一，工具间相互独立
- 通过 `RunContext[EpubContext]` 共享状态
- 工具的返回值同时承担"下一步该做什么"的引导作用

**信息查询类：**
```python
get_book_info()                # 书籍元信息
list_chapters()                # 章节列表及翻译状态
get_glossary()                 # 术语表
get_translation_progress()     # 翻译进度
list_images()                  # 图片列表
```

**翻译操作类：**
```python
update_glossary()           # 更新术语表
translate_toc()             # 列出目录项
save_translated_toc()       # 保存目录，并同步 nav.xhtml
get_image_base64()          # 读取图片
save_translated_image()     # 保存图片
```

四个章节级工具（`get_untranslated_content` / `store_translation_chunk` / `save_translated_chapter` / `check_chapter_progress`）已随块级 run 一并下线，它们的职责回到 Python 侧（分别是块循环、字典写入、`finalize_chapter`、无消费方）。这不只是搬家：这些工具的 schema 在旧实现里**每个请求都要重发一遍**，约 1.8k tokens。

**Python 侧函数（不是工具）：**
```python
finalize_chapter(ctx, index)      # 校验完整性并写回 book / 缓存 / 进度
finalize_epub(ctx, output_path)   # 收尾写盘
merge_glossary(ctx, terms)        # 并入术语表并落盘（走 update_progress）
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
    chunk_attempts: Dict[int, Dict[int, int]]      # {章节: {块号: 已尝试次数}}
    incomplete_chapters: Dict[int, str]            # 保存了但判定不完整 {章节: 原因}
```

派生状态一律由方法实时计算，不额外维护副本：`chunk_count` / `pending_chunks`（缺译文的块号）/ `assembled_translation`（按块号排序拼接）/ `source_tag_count`（可选 `block_only`）/ `thin_chunks`（块级标签不达标的块号）。块级尝试次数用 `attempts(章, 块)` 读、`record_attempt(章, 块)` 记。

`EpubContext` 现在**跨 run 存活**：它不再是某一次 run 的 `deps`，而是 Python 侧章节循环的状态容器，块级 run 完全不碰它（无 deps）。

关键方法 `prepare_chapter(index)`：切分章节、重置该章译文、记录分块尺寸到日志，返回分块数。**它是普通方法，不是 Agent 工具**——见下节。**`chunk_attempts` 不在这里重置**：否则每块 3 次 × 章级 3 次 = 9 次，重试次数会相乘。

## 关键设计决策与不变量

标【不变量】的条目改动会导致整章甚至整本书白翻，修改前务必读完对应的证据段。

### 分块与译文流转

**【不变量】切分必须由 Python 完成，不能暴露给模型。**
`EpubContext.prepare_chapter` 在 run 之前把章节切好放进 `chapter_chunks`，模型只能取、不能重置。曾经把切分做成 Agent 工具，模型在翻译中途重新切分会清空已攒的译文，导致该章永远保存不了。

**【不变量】分块发放必须是非破坏性的。**
Python 按 `pending_chunks()` 决定发哪块，写入靠 `chunk_index` 定位（`chunk_translations[章][块号]`），译文按块号排序拼接；作废重发只清译文、**绝不动 `chapter_chunks` 里的原文**。章级重试前只清 `thin_chunks` 的译文，通过校验的块原样保留、不重复花钱；`prepare_chapter` 也移出了重试循环，避免重切把已译好的块清掉（关掉缓存时那些译文就真丢了）。
拆成块级 run 之后这条依然是核心：模型不再参与调度，但"按块号定位"是 `EpubContext` 跨 run 存活、以及块缓存能只补坏块的前提。
早期版本用 `pending.pop(0)` 队列，一旦某块的 store 调用被输出截断，这块原文就永久消失了——模型只能跳过它继续下一块（静默漏译），或者整章重来。日志实测一次运行里 3 个章节各丢 1 块，而全章标签比例仍在 86% 以上，比例型指标结构上抓不住这种 1/10 的丢失（换成 `MIN_BLOCK_TAG_RATIO` 也一样：丢 1/10 块只掉到 90%）。

**分块器必须能下钻单根元素。**
`EpubTools._atomize` 递归拆分：超过 `INPUT_MAX_TOKENS` 的元素先产出起始标签、递归处理内部、再补结束标签。
早期版本只遍历 `body.children`，而 calibre 导出的 EPUB 常把整章包在一个 `<section>` 里——于是"切分"后整章仍是一个 2.5 万 token 的块。这个块喂给模型后，输出被 `max_tokens` 截断 → 工具调用参数的 JSON 不完整 → 模型退化成把 `<tool_call>` 当普通文本吐出来，控制台上只显示一行"模型输出了文本形式的工具调用"，根因完全看不出来。
所有分块拼接后与原 body 内容完全一致，这是分块器的正确性约束。

**`INPUT_MAX_TOKENS` 必须显著小于 `OUTPUT_MAX_TOKENS`。当前取值 `5000 / 32768`。**
译文 + 完整 HTML 标签 + JSON 字符串转义叠加后输出会放大：拿现有缓存和日志里配对的 500 组"发放/写入"实测，输出/输入 token 比中位 1.32、p90 1.68、p99 1.87、最大 1.99（其中 JSON 转义只占 +1%，主要来自中文 token 密度）。此外推理 token 也计入 `max_tokens` 却不出现在 `result.output` 里，因此"输出看着不长"并不代表没被截断。
按最坏比例 2.0 算：`5000 × 2 + 12000(推理) ≈ 22000 < 32768`。12000 是成功翻译样本里见过的最大推理规模（约 3 万字符）再留余量——stepfun 至今没在 `usage.details` 里报过 `reasoning_tokens`（details 只出现过 `cached_tokens`），推理规模只能从 `reasoning_content` 字符数间接观测。注意这只覆盖正常收敛的样本：2026-08-26 还定性了同一块内容推理膨胀到 10 万字符、把整个输出预算烧光的「推理跑飞」形态，那属于供应商侧行为，预算公式救不了（见「已知未解决问题」）。
调大 `INPUT_MAX_TOKENS` 能成倍减少分块数，进而线性降低整章的累计输入 token，但**单请求峰值上下文不变**；代价是一旦某块译文超预算被截断，重试同一块还会再次超出，整章会耗尽重试次数。
（拆成块级 run 之前，累计输入是块数的**平方**级——每次请求都要重发已累积的对话。那个平方项连同 message history 一起消失了，见下节。）

### 块级 run 的由来（2026-08 的 400 撞墙）

翻译《Designing Data-Intensive Applications》章节 23（`ix01.html`，索引页，37 块 / 162331 tokens）时，三次尝试全部失败，其中两次是供应商直接返回 400：

```
'max_tokens' is too large: 16384. This model's maximum context length is
262144 tokens and your request has 246773 input tokens
```

**根因不是章节总量超上下文**（162k < 262k），而是"一章一个 `agent.run`"里 message history 无界累积。每块会在历史里留下两份：

| 项 | 约 tokens |
|---|---|
| `get_untranslated_content` 的返回值（原文） | 4900 |
| `store_translation_chunk` 的调用参数（译文） | 4600 |
| 合计 | **≈ 9500 / 块**，且整个 run 期间永不丢弃 |

可用输入窗口 = 262144 − 16384 = 245760，除以 9500 ≈ **26 块就是天花板**。两次失败都精确死在第 26 块（246773 ÷ 26 ≈ 9491），与推算吻合。而那 247k 里真正对翻译第 27 块有用的信息只有 5–8k（术语、上一块接缝、风格），死重率约 97%。

**为什么选"无工具"而不是"压缩历史"**：块级 run 只要还带工具，就要为 10 个工具的 schema 每个请求付一遍约 1.8k tokens，而块级翻译一个工具都不需要。更重要的是，去掉工具顺带消灭了本项目最凶的故障模式——**工具调用参数 JSON 被截断 → 模型退化成把 `<tool_call>` 当文本输出**：没有工具就没有工具参数。

**收益的性质要说清楚**：改造前那 4.34M 单章累计输入里 4.1M 是缓存读（94.6%），因为每次重发同一个增长前缀恰好全部命中 prompt cache。拆 run 之后可缓存前缀只剩 system prompt，块原文永远是新的。按缓存读 1/10 价折算，**账单大约降到 1/2，不是 1/10**。这个改动的真正价值是"长章节从不可能变成可能"，不是省钱。

| | 改造前 | 改造后 |
|---|---|---|
| 第 N 块单请求输入 | 2k + N × 9.5k | **恒定约 8k** |
| 第 26 块 | 247k → 400 撞墙 | 8k |
| 章节 23（37 块） | 不可能完成 | 累计约 296k，正常 |
| 每块请求数 | 约 2.4 | **1** |

### 并发与持久化

**【不变量】同一响应里的多个工具会被并发执行，必须挡住。**
pydantic-ai 在 `_agent_graph.py` 里对一个响应内的多个 tool call 走 `asyncio.create_task` 并发执行（`should_call_sequentially` 为假时），而本项目的工具全是同步函数——会被丢进线程池真正并行。所有工具共享同一个 `EpubContext` 和同一个进度文件，并行就会互相覆盖。因此 `EpubTranslator._run_agent` 用 `agent.sequential_tool_calls()` 包住每一次 run。
实测：一个响应同时发 `update_glossary` + `save_translated_chapter` 时，两个 `load→改→save` 交错，术语表更新被整体丢弃，且短文档只覆盖了进度文件前 339 字节、尾部残留上一版内容，之后 `load_progress` 一直报 `Extra data: line 14 column 2` → `_is_chapter_done` 恒为假 → 每章耗尽重试、全书判定失败。

**【不变量】进度文件的写入必须原子，"读—改—写"必须在锁内。**
`CacheManager` 用 `threading.RLock` 串行化进度读写，`_save_locked` 先写 `{key}.json.{pid}.tmp` 再 `os.replace`（`write_text` 会先截断，交错写入就会撕裂文件）。跨调用的改动走 `update_progress(cache_key, mutate)`，把改动塞进同一个临界区；单独 `load_progress` → 改 → `save_progress` 的写法会丢更新，已从代码里清除。
`load_progress` 另外容忍历史遗留的尾部残留：用 `raw_decode` 取首个完整文档并立刻重写成干净文件。

**章节是否落盘由 `finalize_chapter` 的返回值决定，不再回读进度文件。**
旧实现在 run 结束后用 `_is_chapter_done` 回读进度来判断这一章成没成，于是进度文件损坏或被删时每章都会被判成"未落盘"而白白耗尽重试次数（当时的兜底是退回 `ctx.saved_chapters`）。现在保存动作本身在 Python 侧，`finalize_chapter` 直接返回 `(是否完整, 说明)`，判定不依赖任何外部状态。`ctx.saved_chapters` 仍在保存成功时登记，但已没有读取方，属于留待清理的遗留字段。

**进度 JSON 第一个键是 `book_name`。**
缓存键是"文件绝对路径 + 目标语言"的 MD5，光看文件名认不出是哪本书，所以把书名（EPUB 文件名去后缀，与日志文件同名）记在最前面。旧缓存缺这个字段时默认空串，并在下次运行时回填并立即落盘——整本已翻完时后面不会再有 `save_progress` 把它写出去。

### 完整性判定

**校验用标签数而非字符数。**
中文译文字符数天然比英文原文少一半左右，按字符判断会大量误报；HTML 标签要求原样保留，标签数与语言无关。

**【不变量】标签必须分块级和内联两个口径，只有块级能判失败。**
块级标签（`p` / `div` / `h1`–`h6` / `li` / `table` / `img` …，集合见 `agent_tools._BLOCK_TAGS`）与段落一一对应，少一个就是真的少一段内容，因此它是硬指标：低于 `MIN_BLOCK_TAG_RATIO`（0.8）即判漏译。内联标签（`a` / `em` / `span` / `strong` / `br`）模型会系统性地吞掉而正文一字不缺，低于 `MIN_INLINE_TAG_RATIO`（0.8）只在返回值和日志里点名，不阻塞保存。`img` 归进块级——丢一张图是真的内容缺失。

早期只有一个全标签口径（`MIN_TAG_RATIO = 0.8`），把"整段没译"和"内联标签被吞"混为一谈。《Marriage and Morals》第 5 章因此卡死：正文完整译完，只丢了 5 个脚注 `<a>` 和 2 个 `<em>`，全标签 49/63 = 77.8% 擦线掉下 0.8 就被判漏译。

| 只数开标签 | div | h2 | p | span | strong | br | a | em |
|---|---|---|---|---|---|---|---|---|
| 原文 | 1 | 1 | 8 | 9 | 2 | 1 | 8 | 2 |
| 译文 | 1 | 1 | 8 | 9 | 2 | 1 | **3** | **0** |

新口径下这一章块级 10/10 直接通过，内联 15/22 记一行告警。

`_count_tags(html, block_only=...)` 的两个口径出自同一次 `findall`，**`block_only=False` 的结果必须与历史上的 `_TAG_RE` 逐字节一致**（都要求闭合 `>`、都同时数开闭标签、都不匹配 `<!--` / `<?xml`），否则日志里 `DATA save_chapter` 的 `tags` / `source_tags` 就不能和历史数据比了。点名"少了哪些标签"的 `_missing_tag_names` 只比开标签：闭标签是镜像，两种都数会让数字翻倍。

**块内漏译在块级校验器里当场判定，块缺失在 `finalize_chapter` 硬拦。**
`validate_chunk` 比对该块译文与原文的**块级**标签数，不达标就直接重译同一块——重试的输入是同一块原文，Python 手上一直有它，不存在"不知道漏的是哪一段"。`finalize_chapter` 侧"每块都必须有译文"是硬规则、不设放行次数：放行等于把漏译静默写进成品。

**【不变量】返回值不能承诺机制上做不到的事。**
**这条在块级路径上已经消失**：没有工具，也就没有工具返回值去许愿。补译由 Python 的重试循环实打实地执行——同一块原文原样再发一次，`build_chunk_prompt(retry=True)` 附上"上一次的译文被判定不合格"的说明。它对目录 / 图片工具仍然适用：那两个阶段的工具返回值依然是模型判断"下一步做什么"的唯一依据。
留下当初的证据，因为它解释了为什么不能回退到工具型块循环：旧版 store 说"请补译"、docstring 说"再次调用会重新拿到同一块"，但 `pending_chunks` 只统计"完全没有译文"的块，`get_untranslated_content` 永远不会再发放它——承诺是假的，模型拿不回原文，只能在工具之间转圈。后来用 `take_reissue_chunk` 兑现这句承诺（作废一个块级标签不达标的块的译文、重发原文、每块只给一次机会），并要求 `store_translation_chunk` / `check_chapter_progress` / `save_translated_chapter` 三方口径一致——一个说"可以保存了"、另一个说"还得重译"，模型就会在两者之间转圈。这套记账（`chunk_warned` / `chunk_reissued` / `take_reissue_chunk` / `reissuable_chunks`）随工具一起删掉了，换成 `chunk_attempts` 一个计数器。

**【不变量】判定不完整的章节不写进 `completed_chapters`。**
全章块级标签仍不达标时，译文照样写进 book（部分译文比整章原文有用）并记入 `ctx.incomplete_chapters`，但不标记完成，从而触发本次重试、下次 `--resume` 重译。曾经"拒绝一次就放行并标记完成"，残缺章节会被 `--resume` 永远跳过。
因此 `finalize_chapter` 只有两个终点：完整保存、或保存但判定不完整。**不给它加"先去重译再来保存"的拒绝路径**——那会让它可能一次都不成功，本章连部分译文都写不进 book。要重译就在调用它之前重译（块级重试就在它前面）。
块缓存是同一条不变量在块粒度上的延伸：**只有通过 `validate_chunk` 的块才写进 `chunks/`**，坏块的译文只留在内存。于是章级重试和 `--resume` 天然只重跑坏块，而不会把"存过就算过"扩散到块级。

**【不变量】保存过的章节，不能让任何入口再对同一章发号施令。**
块级路径上这条**由结构本身保证**：章节级工具全部下线，`finalize_chapter` 是普通函数，Python 调完就往下走，模型没有任何机会对已保存的章节再做动作，`finished_chapters` / `_already_finished` 守卫连同工具一起删掉了。
证据段留着，因为它是"不要回退到工具型块循环"的核心理由：当时 `save_translated_chapter` 的两个终点都登记 `finished_chapters`，此后 `check_chapter_progress` / `get_untranslated_content` / `store_translation_chunk` / `save_translated_chapter` 一律走 `_already_finished`，回同一句"本章已结束，不要再调用任何章节工具"。少了这个守卫，save 说"本次任务到此结束"，另外两个工具同时说"都已完成，请调用 save_translated_chapter"——三方互相打脸且没有一个合法的收尾动作，模型只能在唯一"安全"的工具上转圈，烧到 `request_limit` 为止（实测第 5 章刷了 15 次"已无待译分块"直到用户 Ctrl-C）。术语表工具当时**不加**守卫：日志里模型有 `save → get_glossary → update_glossary` 的顺序，拦掉会丢术语。
一句话：多个工具对同一份状态各自表态，就一定会出现自相矛盾的出口。工具越少，这个风险越小；块级路径把它降到了零。

**工具的错误不计入重试，只有 `request_limit` 兜底。**
这条只对目录 / 图片工具适用了。工具的错误是 `return "错误：…"` 而不是 `raise ModelRetry`，pydantic-ai 视为调用成功，既不计入 `max_tool_retries` 也不中断 run，因此模型可以在同一个错误上无限循环，唯一的刹车是 `UsageLimits(request_limit=MAX_REQUESTS)`。这条约束直接推导出诊断日志里的"每次工具调用都留一行"不变量。
块级路径没有这个问题：那里没有工具返回值，循环由 Python 的 `while ctx.attempts(...) <= MAX_CHUNK_RETRIES` 控制，次数硬上限。API 层的异常（超时、5xx、内容审查 451）会被 `translate_one_chunk` 捕获并**计入该块的尝试次数**——好处是不会无限重试，代价见"已知未解决问题"。

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
`openai_reasoning_effort` 会被 pydantic-ai 直通成请求里的 `reasoning_effort`——2026-08-26 用 httpx event_hooks 抓包实证：`models/openai.py:659` 的参数映射原样透传，profile 的剔除名单只针对 o 系/gpt-5 的 temperature 等，碰不到它。step_plan 的思考模式**无法关闭**（官方文档只有 low/medium/high 三档强度，实测任何写法都不影响推理照跑），因此 `settings.REASONING_EFFORT` 只控制「发不发、发哪档」：None/空串/"none" 不发送，当前取 low——对正常内容能省推理时间，但对「推理跑飞」的病态块无效（实测 low 档照样膨胀到 9 万字符，见已知问题）。
**输出上限同时发两遍**：`max_tokens=OUTPUT_MAX_TOKENS` 被 pydantic-ai 发成 `max_completion_tokens`，而 stepfun 这类只认 `max_tokens` 的供应商靠 `extra_body={"max_tokens": OUTPUT_MAX_TOKENS}` 兜住（`extra_body` 在 `models/openai.py:674` 直接合并进请求体）。两个字段值相同、谁认哪个都生效，避免供应商命名差异把上限静默丢掉。

**关于 `finish_reason=length` 的正确读法**（推翻了早期结论）：早期文档写"日志里仍出现 `length` 就说明两个字段都没被认"，这是反的——`length` 恰恰是**有上限在生效**的证据，只是无法从 finish_reason 区分截断发生在我们发的 16384 还是供应商自己的默认值。
现有日志里 `length` 一共出现过 1 次（《The Design of Everyday Things》第 10 章，15 次响应中的第 2 次）：那次响应没能发出任何工具调用，pydantic-ai 补一轮请求后自行接上，本章 5 块全部写入、第 1 次尝试就保存成功（`tags 1142/1252`、`thin_chunks []`）。
**结论：在工具型路径上单次 `length` 是可恢复的，真正致命的是截断落在 `store_translation_chunk` 的参数中途**——参数 JSON 不完整，模型就会退化成把 `<tool_call>` 当文本输出（另一份日志里连续发生过 6 次）。这个致命形态随工具一起消失了：块级输出是纯文本，截断只会让译文短一截，不会让它变成假工具调用。
块级路径还把 `length` 的归因精确到了块：`validate_chunk` 直接把 `finish_reason=length` 判为失败并重译同一块，日志里那一行就写着是第几块第几次尝试（旧实现只有章级聚合的"章节 6 有 1/5 次响应被截断"，看不出是哪块）。

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
   ├─ create_epub_agent()  → 注册 epub_toolset（目录 / 图片用）
   ├─ create_chunk_agent() → toolsets=[]，块级翻译用
   └─ new EpubTranslator(agent, chunk_agent)
   ↓
3. translator.translate_epub()
   ├─ init_logger → ~/.auto-epub/logs/{书名}_{时间戳}.log
   ├─ 读取 EPUB、检测源语言
   ├─ 加载缓存进度（回填 book_name）
   ├─ 创建 EpubContext
   └─ 回填已完成章节的译文（缓存内容缺失的章节会被踢回待翻译）
   ↓
4. 逐章循环（Python 控制）
   ├─ ctx.prepare_chapter(index)        # 切分一次，章级重试不重切
   ├─ translate_chapter_chunks()        # 块循环，每块一次独立 run
   │    └─ 每块：查块缓存 → 命中即跳过；未命中则
   │       build_chunk_prompt(原文 + 接力包) → chunk_agent.run_stream()
   │       → clean_model_html → split_terms_block → validate_chunk
   │       → 通过：写 chunk_translations[章][块] + 块缓存 + merge_glossary
   │         不通过：重试同一块（每块共 MAX_CHUNK_RETRIES + 1 次）
   ├─ finalize_chapter()                # 全章复查 → 写 book / 章节缓存 / 进度
   └─ 未通过且仍有块有额度则章级重试（额度用尽直接收工）
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
~/.auto-epub/cache/
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
    ├── chunks/
    │   └── {md5(chapter_id)}/
    │       └── {md5(块原文)}.html      # 内容哈希寻址，见下
    └── images/
        └── {md5(image_name)}
```

缓存键为 `md5(文件绝对路径 + 目标语言)`。

**缓存与日志统一放 `~/.auto-epub/`（`cache/` 与 `logs/`）**，不再散落在运行命令时的工作目录里。缓存键只含书籍绝对路径和语言、与缓存目录位置无关，所以搬目录不影响 `--resume`。旧版目录（工作目录下的 `.epub_translation_cache` / `.epub_translation_logs`）由 `settings.migrate_legacy_dir` 在首次创建 `CacheManager` / `TranslationLogger` 时**整体搬移**过去，规则刻意保守：只在「旧目录存在且目标不存在」时整目录 `shutil.move` 一次；目标已存在（新版已跑过）就按遗留物处理留在原处；搬移失败静默跳过——最坏情况只是丢一次断点续传，绝不能让迁移阻塞翻译。CLI 的 `clear` 命令指定书籍路径时按缓存键清除单本，**省略书籍路径时清空整个 cache 目录**（`CacheManager.clear_all`），后者是唯一会批量删缓存的入口。

**块缓存用内容哈希而不是块号寻址**，因此 `models.py` 里不需要任何新字段：`INPUT_MAX_TOKENS` 调整或原书更新导致切分变化时，哈希自然不匹配 → 命中失败 → 重译，既不需要版本号，也不可能把 A 块的译文错位读成 B 块的。文件存在即进度，所以**不必为每块写一次进度文件**（否则一章 37 块就是 37 次全量 JSON 重写）。`clear_cache` 已经 `rmtree({cache_dir}/{cache_key})`，新目录自动被覆盖。

**断点续传逻辑：**
1. 加载进度文件，取出 `completed_chapters`
2. 逐章从 `chapters/` 读回译文写进 book 对象
3. 进度说已完成但缓存文件丢失的章节，从 `completed_chapters` 中移除并重新翻译——否则会静默输出原文
4. 只对不在 `completed_chapters` 里的章节跑块循环
5. 章内再按块续：每块先查 `chunks/`，命中就完全不发 API。因此上次跑到一半被 Ctrl-C 的长章节，重跑只补剩下的块，不会从头重译

## 诊断日志

翻译失败在控制台上往往只留一行错误。`logger.py` 把细节写入 `~/.auto-epub/logs/{书名}_{时间戳}.log`：

- 每章每次尝试的分隔行、切分后的分块尺寸（`最大分块 N tokens，合计 M tokens`）
- **【不变量】每块每次尝试都留一行**（`logger.chunk_result`）：块号 / 尝试次数 / `chars=原文→译文` / `block_tags=译/原(比例)` / token 用量 / `结束原因` / 失败原因，命中块缓存与"无可译文本原样透传"也各留一行。
  块级 run 没有工具，也就没有"工具调用日志"可留，这一行是块级路径上发现空转与静默失败的唯一手段：**写不出这一行就说明代码路径漏了记账**。所以连"块级重试额度已用尽、本次不再请求"这种"什么都没干"的分支也必须留一行——否则它在日志里是一片空白。
- 每次工具型 run（目录 / 图片）的输出长度、输出片段、token 用量、实际发起的工具调用序列
- **【不变量】每一次工具调用都留一行**（目录 / 图片阶段）。本身有专门日志的工具（保存目录 / 保存图片）保持原样，其余工具走 `logger.tool_call`（INFO），所有错误与空转 return 走 `logger.tool_error`（WARN + VERBOSE 控制台）。
  原因见"工具的错误不计入重试"：这类错误天然可以无限循环。实测一章 208 token 的 titlepage 空转掉 128 个请求、4 分 01 秒后抛 `UsageLimitExceeded`，而日志里只有一行"发放块 0"，事后完全无法判断它在调什么。空转时刷屏的重复行正是需要的证据，且被 `request_limit` 天然限量。
  这条历史教训还留下一个补丁：`run_result` 原先只在 run 成功返回时才写，目录与图片阶段则**根本没有调用它**——run 正常返回但什么也没保存时，日志里一片空白。现在 `_run_agent` 对两个阶段都记录用量、结束原因，并在输出里出现文本形式工具调用时另打一条。
- 诊断内容按 `{日志名}_{标签}_try{章级尝试}_{类型}.{后缀}` 完整落盘（不截断）：`rejected.html` 是判定漏译的全章译文，`chunk{块号}_a{块级尝试}.html` 是校验失败的块级原始输出（纯文本输出的失败样子五花八门——围栏、前言、截断、整段漏译，片段看不出结尾有没有被截断，所以完整存一份），`leaked.txt` 是文本形式工具调用的原始输出
- `DATA` 前缀的 JSON 行（`save_chapter` / `chapter_failed` / `finish`），便于脚本统计失败分布。`save_chapter` 里 `tags` / `source_tags` 是全标签口径（与历史日志可比），另有 `block_tags` / `source_block_tags`（判定依据）、`inline_tags` / `source_inline_tags`、`thin_chunks`（块级不达标的块号）、`chunk_attempts`（`{块号: 尝试次数}`，取代了旧的 `reissued_chunks`）
- token 用量里带 `usage.details`：`reasoning_tokens` 计入 `max_tokens` 却不出现在 `result.output` 里，是"输出看着不长却被截断"的隐形消耗者，所以只要供应商报了就记下来（stepfun 至今只报 `cached_tokens`，没报过 `reasoning_tokens`）
- 每次模型响应的 `finish_reason` 序列（归一化值 + 括号内供应商原值）。`length` 是输出被 `max_tokens` 截断的直接证据，出现时额外打一条 WARN 并提示控制台；读法见"关于 `finish_reason=length` 的正确读法"
- 每次 `finalize_chapter` 拒绝保存的原因、判定不完整的章节和块号

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

**章节样式丢失（已修复，2026-08-27）。** `finalize_chapter` 原先写入的是 body 级分块的拼接结果，`<html>` / `<head>` 外壳及其中的 CSS 链接会丢失。**已修复**（2026-08-27）：保存时用原章节的 soup 做模板只替换 body；但光这样还不够——ebooklib 写盘（EpubHtml.get_content）会用自家模板重建整个文档，head 只输出 item 上注册过的 links，因此必须先从原始字节（chapter.content，不能用已被模板化的 get_content()）把样式表 `add_link()` 注册回去。端到端验证：重建 EPUB 后章节 head 中 `../Styles/stylesheet.css` 完整存在；缓存同步存全量 HTML。

**传输层异常和内容不合格共用同一份重试额度。** `translate_one_chunk` 的 `except Exception` 把超时、5xx、内容审查 451 一律记成"该块的第 k 次尝试失败"（`chunk_translator.py:429-439`）。好处是任何异常都不会变成无限重试；代价是一个块可能三次尝试全部死在传输层，**从头到尾没拿到过一次译文**，却已经耗尽额度。2026-08-25 冒烟跑里章节 12 的块 0 就是这样：三次全是 `ModelAPIError: Request timed out.`。修复方向是把异常分成"重试可能有救"（超时、5xx、429）和"重试必然同样结果"（451 内容审查、400 参数错），前者不计入 `chunk_attempts` 但另设一个独立的传输重试上限，后者立即放弃、不浪费后两次。
**供应商侧不可控失败直接保留原文（2026-08-27 实施）。** 针对 `UnexpectedModelBehavior: Exceeded maximum retries` 等供应商侧不可控失败（推理跑飞、内容审查等），`translate_one_chunk` 在 `except` 中用 isinstance 识别到此类异常（含子类 ContentFilterError）时，直接将原文作为译文保留、标记为通过（`passthrough=True`），不再消耗后续的块级重试额度。避免在同一病态块上白白烧完 `MAX_CHUNK_RETRIES` 次额度，节省整章翻译时间。相关代码在 `chunk_translator.py:451-461`。


**`TIMEOUT = 60` 对大输出偏紧。** 这是单次 HTTP 请求的超时，而 OpenAI SDK 自己还会重试两次，所以**一次"块级尝试失败"的实际墙钟成本约 183 秒 ≈ 3 × 60**：冒烟跑里章节 12 的块 0 三次尝试分别落在 21:35:37 / 21:38:40 / 21:41:43，整整烧掉九分钟才判定失败。同一次跑里章节 15 唯一的块第 1 次超时、第 2 次成功（输出 8203 tokens），第 2 次从上一行到落地共 104 秒——如果它内部也重试过一轮，那真正成功的那次请求约 44 秒，正好压在 60 秒线上；这一步是推断，日志只记了 104 秒的总耗时。反过来看旧架构《DDIA》那次跑的 231 个成对样本：中位 21 秒、p90 33 秒、p99 94 秒，只有 3/231 超过 60 秒，所以 60 秒历史上是够用的，只是对单块大输出没有余量。当时只有这一次跑的 8 个超时样本，不足以断定；同日深夜已用探针定案并追加实测数据（见下一条）。

**`TIMEOUT = 60` 偏紧已定案：瓶颈是隐藏推理抬高的首 token 延迟 + 真实总耗时 62~68 秒。** 2026-08-25 深夜用《DDIA》章节 23 块 1（提示词 5074 tokens）做两组探针：①流式成功，总 67.5 秒，但**首个 delta 要等 51.3 秒**——step-3.5-flash 的隐藏推理全部做完才吐第一个字——其后 80 个 delta 最大相邻间隔仅 0.7 秒、12072 字符一气呵成；②非流式直连（timeout=600）62.3 秒 `finish_reason=stop`、译文完整。即单请求真实墙钟 ≈ 62~68 秒，生产环境的 60 秒线恰好压在它下面，每次尝试 60s × (1 + SDK 内部重试 2 次) = 183 秒判死，与 `…235024` 日志的分秒完全吻合。同一批块当天下午（旧架构 `…171643`）每块只要 20~40 秒，说明晚上全军覆没是**供应商变慢叠加 60 秒余量为零**，不是块级重构改出来的 bug。**附带发现**：该模型无视 `"thinking": {"type": "disabled"}` 与 `reasoning_effort="low"`（实测 `reasoning_content` 长达 22663 字符，而 `usage.reasoning_tokens` 记 0，供应商未单独记账）；推理更长的抽样会把 `OUTPUT_MAX_TOKENS` 整个吃穿 → 正文为空或截断 → pydantic-ai 输出校验重试后抛 `UnexpectedModelBehavior: Exceeded maximum retries (1) for output validation`（实测 144.8 秒 ≈ 2 × 72 秒）——**这条调大 TIMEOUT 治不了**。**已实施（2026-08-25 深夜）**：块级 run 改走 `agent.run_stream` 收全量文本再走原有校验（`translate_one_chunk`），超时语义从「总量 ≤ TIMEOUT」变成「等响应头 / 相邻 delta 静默 > TIMEOUT」，对实测 0.7 秒的流间隔极其安全；`TIMEOUT=180`（≈3.5 倍首 token 延迟余量），并把 provider 的 `max_retries` 接出为 `MAX_RETRIES=0`——HTTP 层不再静默重试，传输失败全部落进块级循环记账。生产路径实测（同块）：两次 `finish_reason=length` 截断重试后第 3 次 `stop` 通过；截断源于推理抽样波动，非流式下同样存在、只是表现为输出校验异常，重试可兜住，某本书频繁 length 截断就按日志提示调小 `INPUT_MAX_TOKENS` 或换模型。**流式日志注意点**：step_plan 每个 SSE delta 都带 usage 且被 pydantic-ai 累加，流式路径上 `chunk_result` 的输入/输出/缓存读 token 数严重虚高（实测出现 5400 万），判读以 chars / block_tags / finish_reason 为准。

**2026-08-26 补充（连坐误拒 + 输出预算，均已修复）：** pydantic-ai 见到 `finish_reason=length` 会自动补发一次请求（`_agent_graph` 的 ModelRetry，Agent 默认重试 1 次），一次块级尝试因此留下多条响应；而校验与告警原先取**全部**响应的结束原因，被丢弃的截断响应会把第二条完整译文连坐否决——《DDIA》章节 23 块 1 实测 length/stop 被拒，转储里术语块齐全。已新增 `Logger.final_finish_reasons` 只看最后一次响应，校验与截断横幅都改用它。同日把 `OUTPUT_MAX_TOKENS` 提到 32768（供应商实测接受 24576/32768），从源头减少推理顶爆预算的截断。

**`failed_chapters` 只增不减。** `_mark_failed` 只 append，没有任何地方在后续运行成功后把章节 ID 移出这个列表。于是 2026-08-25 那次跑收尾时打印 `失败章节 2 个: ch013, ch016`，而 ch016（章节 15）本次明明翻译成功、已经进了 `completed_chapters`。只影响收尾报告的准确性，不影响续译（`_pending_chapters` 只看 `completed_chapters`）。修复方向是在 `finalize_chapter` 成功后顺手从 `failed_chapters` 里摘掉，或者在 `_finalize_and_report` 里按 `completed_chapters` 过滤一遍。

**某些内容会被供应商永久拒译。** 《Marriage and Morals》第 12 章有一块稳定触发 `status_code: 451 … censorship_blocked`，旧架构（`…20260818_114902.log`）在**同一块**上报的是同一个 451——这是供应商侧的内容过滤，不是本项目的缺陷，换模型或换供应商才有用。当前行为是这一块耗尽三次尝试、整章拒绝保存、如实报失败，这是正确的失败方式（红线 5），但白花了两次请求。

**step_plan「推理跑飞」（2026-08-26 定性）。** 《When Money Destroys Nations》章节 14 块 1（21283 字符）会让 step-3.7-flash 以高概率陷入隐藏推理死循环：`reasoning_content` 膨胀到 9~11 万字符、把 32768 输出预算整个烧光、正文 0 字符、`finish_reason=length`；pydantic-ai 对 length 自动补发一次后仍然全空，对外抛 `UnexpectedModelBehavior: Exceeded maximum retries (1) for output validation`。
同日 12 次受控实测（同提示词、近同参数）仅 1 次自然收敛（141 秒 stop、译文 9043 字符、推理约 3 万字符），其余全部跑飞；流式比非流式更糟（5/5 全灭），`reasoning_effort=low`、`enable_thinking=False`、极简强指令提示词全都拦不住。
根因在供应商：step_plan 强制开启思考，且 `max_tokens` 把思维链和回答一起限长（对照：阿里云百炼托管的同名模型默认关闭思考、思维链不计入 max_tokens，但那是另一个需要单独开通的端点）；本机 httpx 抓包证明 pydantic-ai 参数传输无误，锅不在客户端。
**已实施的缓解**：`settings.STREAMING=False` 回切非流式（收敛概率相对更高的路径）、`TIMEOUT=360` 给慢收敛样本留余量、`REASONING_EFFORT=low` 压正常内容的推理开销。**遗留**：病态块没有软件侧解法，重试等于抽签；日志特征是「chars=N→0 + finish=length + UnexpectedModelBehavior」，某本书频繁出现就直接换供应商，别再烧额度。

**~~无测试~~（2026-08-27 起已有单元测试）。** `tests/` 下建立了单元测试套件（`python -m pytest` 运行），历史结论仍以真实翻译的诊断日志为准；套件的布局与红线覆盖关系见下方「单元测试」一节。

## 单元测试

**运行方式与边界：** `python -m pytest`（配置在 `pyproject.toml` 的 `[tool.pytest.ini_options]`）。套件**不发 API、不写真实缓存目录**：cache/log 一律 Mock 或 None，EPUB 对象在内存里构造；涉及临时目录的少数用例因沙箱权限受限标注 skip，其余全部纯内存执行。

| 文件 | 覆盖 |
|------|------|
| `tests/test_models.py` | 四个 Pydantic 模型的默认值、序列化往返 |
| `tests/test_settings.py` | 红线 10（INPUT/OUTPUT_MAX_TOKENS）、重试常量、标签比例阈值、提示词占位符、`migrate_legacy_dir` 边界规则 |
| `tests/test_config.py` | `_ModelProvider` 与 `.env` 加载入口 |
| `tests/test_cache_manager.py` | `_decode` 尾部残留容忍（进程内缓存撕裂修复口径）；文件型用例待沙箱放开后补齐 |
| `tests/test_logger.py` | ConsoleLevel 分级、chunk_result 结构化 JSON 行、finish_reason 只看最后一条响应、usage/details 提取、每次工具调用一行 |
| `tests/test_epub_tools.py` | 语言元数据读写、章节/导航文档按内容识别（page-list / landmarks 不参与翻译）、xml 解析器保留 `<head>`、**分块器拼接恒等**（所有块拼回等于原 body 内容）、单 `<section>` 包裹整章时下钻切开 |
| `tests/test_chunk_translator.py` | clean_model_html 剥围栏/前言、边界标记不以 `<` 开头的原因、术语块摘除与幻觉键核对、接力包优先级（接缝 > 术语 > 风格锚点）与硬上限砍断、validate_chunk 全分支（配平不做检查、内联只告警、img 计块级）、缓存命中 / 无文本透传 / 额度用尽三条零 API 分支（红线 6、8、9） |
| `tests/test_agent_tools.py` | 标签计数两口径一致性、目录 collect↔apply 严格同序、EpubContext 状态机（乱序写入按块号还原、thin_chunks 只看块级、`prepare_chapter` **不重置 attempts**）、merge_glossary 先到先得并走 update_progress、finalize_chapter 保存闸门（有 pending 块拒绝且不写进度、判定不完整照写 book 但不进 completed） |
| `tests/test_translator.py` | 待译章节过滤（匹配键是 `get_id()`）、输出路径生成、失败章节幂等标记、缓存恢复与"缺失即踢回重译"、chunk_agent 惰性创建只建一次 |
| `tests/test_client.py` | 两个 Agent 工厂的系统提示词占位符替换、**工具集清点**：目录/图片工具齐全且章节级工具没有复活 |
| `tests/test_cli.py` | `-q` 与 `-v` 互斥、扩展名校验顺序、clear 缺 `-l` 报错、version 输出 |
| `tests/test_concurrent_manager.py` | 未投入主流程的并发控制器公共行为：结果保序、单任务异常被捕获不炸整批、max_workers 串行上限 |

**写新测试的约定：** 行为断言经由公共接口（同 TDD 原则），不断言私有实现细节；凡属上面红线表里的不变量，改动时必须有对应测试先红后绿。

**提交闸门：`git commit` 必须先过全套单元测试（2026-08-27）。**
钩子脚本随仓库版本化在 `.githooks/pre-commit`，用 `git config core.hooksPath .githooks` 启用——git 出于安全从不执行仓库目录里的钩子，`core.hooksPath` 又只存在本机 `.git/config`，所以这步要在每个克隆手动做一次（命令见 CLAUDE.md）。钩子优先用项目 venv 的 Python（`.venv/Scripts/python` → `.venv/bin/python` → PATH 里的 `python` 依次回退，系统 Python 可能不满足 requires-python >= 3.10）跑 `pytest -q`，退出码非 0 即拒绝提交。两条路径均已在真实提交上验证：全绿放行、模拟失败阻断且 HEAD 不动。绕过方式 `--no-verify` 留给明确知情的场景；套件约 2 秒，日常没有绕过的理由。

## 未来扩展方向

### 1. 让目录 / 图片工具的错误进入重试计数

把明显写错的参数（条目数不符、图片名不存在）改成 `raise ModelRetry`，`max_tool_retries=3` 才会真正接管，不必等 `request_limit` 烧满。代价是要区分"模型写错"和"状态本就如此"，后者不该消耗重试。**只对目录 / 图片这两个还在用工具的阶段有意义**；章节正文已经没有工具了，那条路上的失败由 `chunk_attempts` 计数（另见「已知未解决问题」里传输错误与内容失败共用额度的那条）。

### 2. 并发翻译

`concurrent_manager.py` 已提供 asyncio 并发控制和速率限制，但当前流程是串行的。块级 run 之间没有 message history，天然适合并发——挡在前面的是两件事：接力包要读上一块的译文（可以退化成只带术语和风格锚点），以及 `EpubContext.chunk_translations` / `glossary` 现在没有并发写保护。进度文件那一侧已经在锁内原子写入，术语表需要合并策略。

### 3. 添加新工具

```python
@epub_toolset.tool
def translate_metadata(ctx: RunContext[EpubContext]) -> str:
    """翻译书籍元数据（标题、作者等）"""
    ...
```

### 4. 跨书籍共享术语表

当前术语表按书缓存。系列作品可以考虑全局术语表预加载。

## 总结

| 层 | 负责 |
|----|------|
| **EpubTranslator** | 章节循环、切分时机、章级重试、写盘时机 |
| **chunk_translator** | 块循环、接力包、块级校验与重试、块缓存 |
| **chunk_agent**（无工具） | 一块原文 → 一块译文，纯输入输出 |
| **epub_agent**（带工具集） | 只负责目录与图片的自主调度 |
| **Toolsets** | 原子化的 EPUB 操作 + 保存护栏 |
| **EpubContext** | 跨 run 存活的共享状态（分块、译文、术语、尝试次数） |
| **CacheManager** | 进度持久化、章节缓存、块缓存（内容哈希寻址） |
| **TranslationLogger** | 失败可回溯 |

划界的原则是：**凡是"错了会导致整章白翻"的决策，都放在 Python 侧**（切分、发放哪一块、写盘、完整性判定、重试计数）；模型只负责它真正擅长的部分——翻译文本。章节正文这条路上模型已经不做任何调度，它连自己在第几块都只是被告知而已。
