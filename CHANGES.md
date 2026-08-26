# 更新日志

本文件自 v1.3.0 起记录，更早的历史见 git log 与 `docs/ARCHITECTURE.md` 的演进路线。
条目倒序排列，每条写清【动机 → 改动 → 实测证据】；架构层面的设计决策与不变量
沉淀在 `docs/ARCHITECTURE.md`，这里只记改了什么、为什么。

# v1.3.0

## 2026-08-26 · clear 命令简化， 缓存、日志目录迁至 ~/.auto-epub

### 动机

- 缓存与日志散落在运行命令时的工作目录里，换个目录跑就把 `.epub_translation_cache` /
  `.epub_translation_logs` 又铺一份，项目目录越来越乱。
- `clear-cache` 必须同时给书名和语言才能清一本书，想一键清空全部缓存没有入口；
  命令名也比同类工具啰嗦。

### 改动

| 文件 | 内容 |
|---|---|
| cli.py | `clear-cache` 命令改名为 **`clear`**；书籍路径变为可选参数——**省略时清空全部翻译缓存**并报告清除的书本数，指定书名但漏 `-l` 时明确报错退出 |
| cache_manager.py | 默认目录改用 `settings.CACHE_DIR`；新增 `clear_all()`（清空整个缓存目录，按 32 位 md5 键去重计数，写入临时文件等垃圾一并清掉但不计入书数）；`clear_cache` 补上与其他读写同一把锁 |
| settings.py | 新增 `APP_DIR = ~/.auto-epub`，`CACHE_DIR = APP_DIR/cache`、`LOG_DIR = APP_DIR/logs`；新增 `migrate_legacy_dir()`：首次创建 `CacheManager` / `TranslationLogger` 时把工作目录的旧目录整体搬到新位置。规则刻意保守：只在「旧目录存在且目标不存在」时整目录 move 一次；目标已存在视为遗留物留在原处；失败静默跳过，绝不阻塞翻译 |
| logger.py | 日志写入新 `LOG_DIR` 并触发旧日志目录迁移 |
| 文档 | CLAUDE.md / README.md / docs/{QUICKSTART,FILE_MAPPING}.md 同步命令与新路径；docs/ARCHITECTURE.md 追加该设计决策 |

搬家不影响 `--resume`：缓存键是 `md5(书籍绝对路径 + 目标语言)`，与缓存目录放在哪无关。

### 实测验证（沙箱内冒烟）

- `clear --help`：命令名与可选参数正确；`clear book.epub` 漏 `-l` 报错 exit 1。
- 单书清除只删对应键的 `{md5}.json` + `{md5}/` 目录，其余书原封不动。
- 无参 `clear` 输出「已清除全部翻译缓存（共 N 本书）」，缓存目录清空。
- 首次运行自动搬迁旧缓存 / 日志目录，条目完整；新日志落在新目录。
- `ruff check` / `ruff format --check` 全部通过。

## 2026-08-26 · 块级翻译在慢供应商下全军覆没的修复

### 背景

《DDIA》章节 23（ix01.html 索引页，37 块，162k tokens）晚间连续两轮全块失败：
每条 ✘ ModelAPIError: Request timed out. 之间隔精确 183 秒，而同一批块当天下午
（旧架构跑的）单块只要 20~40 秒。用章节 23 块 1（提示词 5074 tokens）做探针实测，
定案三条根因：

1. **非流式超时口径装不下隐藏推理。** step-3.5-flash 出第一个可见字符之前要把隐藏
   推理全部做完（夜间实测首个 delta 要等 51.3 秒），加上正文流完共 62~68 秒，压在
   TIMEOUT = 60 之下必死；OpenAI SDK 默认还在幕后重试 2 次，所以一条失败日志的成本
   是 3 × 60 ≈ 183 秒——与日志分秒吻合。curl 根路径 1~2.8 秒只证明连通性，测不出
   推理延迟。
2. **输出预算被推理顶爆。** 该模型无视 thinking:disabled 与 reasoning_effort=low
   （实测 reasoning_content 长达 22663 字符，而 usage.reasoning_tokens 记 0，供应商
   未单独记账）。索引页这类密集块的「推理 + 译文」会顶满 16384 触发 finish_reason=length。
3. **截断连坐误拒。** pydantic-ai 见到 finish_reason=length 会自动补发一次请求
   （_agent_graph 的 ModelRetry），一次块级尝试因此留下多条响应；而校验原先取全部
   响应的结束原因，被丢弃的截断响应会把第二条完整译文连坐否决——实测 length/stop
   被拒的那份转储里术语块齐全。

### 改动

| 文件 | 内容 |
|---|---|
| chunk_translator.py | translate_one_chunk 由 agent.run 改为 agent.run_stream 流式收全文：超时语义从「总量 ≤ TIMEOUT」变成「等响应头 / 相邻 delta 沉默 > TIMEOUT」（实测流内最大间隔仅 0.7 秒，总时长不再受限）；截断判定改用 Logger.final_finish_reasons |
| settings.py | TIMEOUT 60→180（≈3.5 倍实测首 token 延迟余量）；OUTPUT_MAX_TOKENS 16384→32768（供应商实测接受 24576/32768，最坏情况 5000×2.0+12000 推理 ≈ 22000 < 32768）；MAX_RETRIES 从死配置接为真配置并置 0 |
| client.py | 显式构造 AsyncOpenAI(max_retries=MAX_RETRIES) 传入 OpenAIProvider——HTTP 层不再静默重试 2 次，传输失败直接抛给块级循环记账（每次尝试留一行日志，红线 9），避免一次真挂起烧满 3 × TIMEOUT |
| logger.py | 新增 final_finish_reasons() 只取最后一次模型响应的结束原因；chunk_result 的「被 max_tokens 截断」横幅同步改用它 |
| 文档 | docs/ARCHITECTURE.md 追加定案数据与已知问题更新；docs/FILE_MAPPING.md 调用链同步为 run_stream |

红线 10 的配套数字随之更新：INPUT_MAX_TOKENS / OUTPUT_MAX_TOKENS = 5000 / 32768。

### 实测验证

- 探针（同块）：流式成功——总 67.5 秒，首个 delta 51.3 秒，其后 80 个 delta 最大相邻
  间隔 0.7 秒；非流式直连（timeout=600）62.3 秒 stop、译文完整；同时复现了推理顶爆预
  算时非流式的 UnexpectedModelBehavior 输出校验失败（144.8 秒 ≈ 2 × 72）——证明光调
  大超时治不了截断。
- 生产路径单块：第 1 次尝试即通过（请求数=1、stop、73.3 秒、block_tags 223/223）。
- 整章回归：《DDIA》章节 23 全部 37/37 块一次尝试通过（块 20 出现一次 length/stop 自
  动补发后正常收下），全书 25/25 章完成出书，全程零超时零误拒。

### 已知遗留（不阻塞，详见 ARCHITECTURE.md「已知未解决问题」）

- 流式路径 chunk_result 的输入 / 输出 / 缓存读 token 数严重虚高：step_plan 每个 SSE
  delta 都携带一份 usage 且被 pydantic-ai 逐条累加（实测出现 5400 万）。判读以 chars /
  block_tags / finish_reason 为准；成本核算待修（取最后一个 usage 事件或用 tiktoken 本地估算）。

## 2026-08-26 · step_plan「推理跑飞」定性与流式回切

### 背景

《When Money Destroys Nations》章节 14 连续失败，日志只见 `UnexpectedModelBehavior: Exceeded maximum retries (1) for output validation` 与「chars=21283→0」，真实响应形态不可见。用原始 chat/completions 探针 + httpx 抓包逐变量隔离后定性：这块内容让模型的隐藏推理以高概率膨胀到 9~11 万字符、烧光 32768 输出预算且正文为零；12 次实测仅 1 次收敛。顺带证实 pydantic-ai 对 `openai_reasoning_effort` 的映射与 profile 剔除均无误（抓包见请求体原样携带），问题出在 step_plan 供应商侧：思考强制开启、max_tokens 连思维链一起限长。

### 改动

| 文件 | 内容 |
|---|---|
| settings.py | 新增 `REASONING_EFFORT`（None/空串/"none" 不发送，当前 "low"）；新增 `STREAMING=False` 回切非流式；TIMEOUT 180→360（非流式总量口径下给慢收敛样本留两倍余量） |
| client.py | `model_settings` 改具名构造后按条件注入 `openai_reasoning_effort`；`enable_thinking` 修正为布尔 False（字符串对认字段的供应商等于没关）；注释同步今日证据 |
| chunk_translator.py | `translate_one_chunk` 按 `STREAMING` 分支：True 走原 `run_stream` 保活收全文，False 走 `agent.run`（两者共用同一套校验 / 记账 / 缓存） |
| 文档 | docs/ARCHITECTURE.md 同步块级翻译描述、预算推导数字、参数直通结论，并在已知问题新增「推理跑飞」定性条目 |

### 实测证据（章节 14 块 1，21283 字符，step-3.7-flash）

- 非流式干净参数：1/3 收敛（141 秒 stop、9043 字符），其余推理 9~11 万字符烧满 32768。
- 流式（生产路径）：3 次尝试全灭，每次内部两次请求均空。
- `reasoning_effort=low` ×3：推理 9.3~9.5 万字符，全部烧满——文档承诺的低档省 Token 在病态内容上不存在。
- `enable_thinking=False`、极简提示词：均无效。
- 抓包：请求体原样携带 `reasoning_effort`/`max_completion_tokens`/extra_body 合并字段，映射链路正确。

