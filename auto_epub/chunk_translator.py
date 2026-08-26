"""
块级翻译：一块原文 → 一次无工具的流式 agent.run_stream → 一块译文

为什么不用工具
--------------
旧实现是"一章一次 agent.run"，模型靠 get_untranslated_content /
store_translation_chunk 循环取块存块。每块会在 message history 里留两份——
工具返回的原文（约 4900 tokens）+ 工具调用参数里的译文（约 4600 tokens），
约 9500 tokens/块，且整个 run 期间永不丢弃。可用输入窗口
262144 - 16384 = 245760，除以 9500 ≈ 26 块就是天花板：实测《Designing
Data-Intensive Applications》章节 23（37 块）两次都精确死在第 26 块，
供应商直接返回 400 "your request has 246773 input tokens"。

改成每块一次独立 run 之后，单请求输入与块序号无关（恒定约 8k），
章节长度不再有上限。顺带消灭了本项目最凶的故障模式——「工具调用参数 JSON
被截断 → 模型退化成把 <tool_call> 当文本输出」：没有工具就没有工具参数。

代价是丢了 message history 带来的上下文。用结构化的"接力包"（build_carryover）
补回来：上一块接缝 + 命中当前块的术语 + 本章风格锚点，硬上限 2000 tokens。
接力包里的术语来自模型在每块译文末尾追加的术语块（split_terms_block /
parse_terms_block），不再靠正则去猜译名边界——理由见 _TERMS_BLOCK_RE 上方。

无工具带来的新风险
------------------
工具调用的参数由结构化通道隔离，纯文本输出没有这层保护，所以这里必须自己防：
1. 模型给译文裹 markdown 围栏、或加"以下是译文："前言 → clean_model_html
2. 模型"顺手"闭合原文没闭合的标签 → 提示词明说 + 校验器**不做**标签配平检查
   （EpubTools._atomize 会把开标签和闭标签拆成不同原子，块本来就可能是半截）
3. 整块没有可译文本（例如只有 </section>）→ Python 侧直接透传，不发 API
4. 同一个底模仍可能习惯性吐 <tool_call> → 当作校验失败项
5. 末尾的术语块属于元数据，必须摘掉才能写进书 → split_terms_block
"""

import re
from typing import Dict, List, Optional, Tuple

from pydantic_ai import Agent, UsageLimits

from .agent_tools import (
    _BLOCK_TAGS,
    _TAG_NAME_RE,
    _count_tags,
    _describe_missing,
    _missing_tag_names,
    EpubContext,
    merge_glossary,
)
from .epub_tools import EpubTools
from .logger import get_logger
from .settings import (
    CARRYOVER_MAX_TOKENS,
    CARRYOVER_SEAM_CHARS,
    CARRYOVER_STYLE_CHARS,
    MAX_CHUNK_RETRIES,
    MIN_BLOCK_TAG_RATIO,
    MIN_INLINE_TAG_RATIO,
    STREAMING,
    TERMS_BLOCK_BEGIN,
    TERMS_BLOCK_END,
)

# 模型偶尔会把工具调用写成纯文本而不走 function calling 通道。块级 agent 根本
# 没有工具，出现这些标记说明它在自己编戏，本块必须作废重译。
LEAKED_TOOL_CALL_MARKERS = ("<tool_call", "<function=", "</function>")

# 原文在提示词里的边界标记。故意不以 "<" 开头：clean_model_html 剥前言时会把
# 第一个 "<" 之前的内容全部丢掉，标记若以 "<" 开头就会混进译文躲过清理。
_SRC_BEGIN = "===原文开始==="
_SRC_END = "===原文结束==="

# 整个输出被 markdown 围栏包住（```html … ```）
_FENCE_RE = re.compile(
    r"^```[a-zA-Z0-9_+\-]*[ \t]*\r?\n(?P<body>.*?)\r?\n?```[ \t]*$", re.DOTALL
)

# HTML 实体，判断"有没有可译文本"时不算内容
_ENTITY_RE = re.compile(
    r"&(?:#[0-9]{1,6}|#[xX][0-9a-fA-F]{1,5}|[a-zA-Z][a-zA-Z0-9]{1,9});"
)

# 模型追加在译文末尾的术语块（见 CHUNK_SYSTEM_PROMPT 第 3 条）。
#
# 为什么不从正文里正则抽「译名(原名)」
# ------------------------------------
# 系统提示词要求专有名词首次出现写成「译名(原名)」，看起来可以免费正则抽取，
# 实际抽不准：原名有括号定界，**译名没有左定界**。目标语言可能像中文一样不分词，
# "他遇见了德·雷纳夫人(Madame de Renal)" 里没有任何词边界能把动词和译名分开，
# 贪婪匹配会抽出 "他遇见了德·雷纳夫人"。改成"必须紧跟标点"也救不了：标点恰好落在
# 十来个字之前时（"…出场了，随后是德·雷纳夫人(…)"）照样会把 "随后是" 收进去。
#
# 而抽错的代价远大于抽不到：build_carryover 会把术语表当成"必须沿用这些译名"写进
# 后面每一块的提示词，一条坏译名会顺着提示词扩散到全章正文；抽不到只是同一个名字
# 在两块里可能不一致。所以改成让模型自己用 "=" 把两边分开——边界由模型给出，
# Python 不再猜——再用"键必须原样出现在本块原文里"把幻觉挡掉。
_TERMS_BLOCK_RE = re.compile(
    re.escape(TERMS_BLOCK_BEGIN) + r"(?P<body>.*?)" + re.escape(TERMS_BLOCK_END),
    re.DOTALL,
)
_MAX_TERMS_PER_CHUNK = 20  # 一块里首次出现的专有名词不该有几十个，多了就是模型加戏
_MAX_TERM_KEY_CHARS = 64
_MAX_TERM_VALUE_CHARS = 32

# 接力包里最多带多少条术语（再多也会被 token 预算砍掉，先做个上限省得白算）
_MAX_CARRYOVER_TERMS = 60


# ---------- 输出清理 ----------


def clean_model_html(raw: str, source: str = "") -> str:
    """把模型的纯文本回复还原成干净的 HTML 片段

    只做"剥壳"，不改内容：markdown 围栏、"以下是译文："这类前言、以及被模型
    抄回来的原文边界标记。source 用来判断译文是否本该以 "<" 开头——原文若是
    纯文本（没有标签）就不能按 "<" 截断，否则整块译文会被清空。
    """
    text = (raw or "").strip()
    if not text:
        return ""

    matched = _FENCE_RE.match(text)
    if matched:
        text = matched.group("body").strip()
    elif text.startswith("```"):
        # 只有开围栏没有闭围栏：输出被截断，或模型忘了收尾
        text = text.split("\n", 1)[1].strip() if "\n" in text else ""

    for marker in (_SRC_BEGIN, _SRC_END):
        text = text.replace(marker, "")

    # 前言：原文以标签开头时，译文的第一个字符也必须是 "<"
    if source.lstrip().startswith("<") and not text.startswith("<"):
        pos = text.find("<")
        if pos > 0:
            text = text[pos:]

    return text.strip()


def _plain_text(html: str) -> str:
    """去掉标签和实体后剩下的文字"""
    return _ENTITY_RE.sub(" ", _TAG_NAME_RE.sub(" ", html)).strip()


def has_translatable_text(html: str) -> bool:
    """这一块里有没有需要翻译的文字

    纯闭标签块（`</section></div>`）、纯数字表格单元格没有可译文本，
    发给模型只会让它加戏（补标签、加解释），Python 直接原样透传更省更稳。
    用 isalpha() 而不是判断某个语种：目标语言是任意的。
    """
    return any(c.isalpha() for c in _plain_text(html))


def split_terms_block(translated: str) -> Tuple[str, str]:
    """把块译文拆成 (正文, 术语块内容)

    术语块必须摘掉：它是给后续分块看的元数据，留在正文里就进了成品 EPUB。
    模型漏写结束标记时按"从开始标记到末尾"兜底——宁可多切掉一点尾巴，也不能把
    半截术语块写进书里；正文真的被切短了，块级标签比例会把这一块判为漏译。
    """
    matched = _TERMS_BLOCK_RE.search(translated)
    if matched:
        body = matched.group("body")
        return (translated[: matched.start()] + translated[matched.end() :]), body

    pos = translated.find(TERMS_BLOCK_BEGIN)
    if pos >= 0:
        return translated[:pos], translated[pos + len(TERMS_BLOCK_BEGIN) :]
    return translated, ""


def parse_terms_block(body: str, source: str) -> Dict[str, str]:
    """解析术语块，返回 {原名: 译名}（与术语表同格式）

    每一条都要求**键原样出现在本块原文里**才收下。模型偶尔会顺手把前文见过的、
    或者干脆想象出来的名字也列进来，而这张表会被 build_carryover 当成硬规则写进
    后面每一块的提示词，一条幻觉能顺着提示词扩散到全章正文。用原文核对是这里唯一
    不依赖模型自觉的检查，也顺带挡掉了它把说明文字写成术语行的情况。
    """
    found: Dict[str, str] = {}
    # 拿去标签、并把连续空白压成单空格的正文当核对底本：EPUB 原文里
    # "Madame de\n    Renal" 这样跨行的名字很常见，直接在原始 HTML 里找会找不到
    haystack = " ".join(_plain_text(source).split())
    for line in body.splitlines():
        line = line.strip().lstrip("-").strip()
        if not line or "=" not in line:
            continue
        original, _, rendered = line.partition("=")
        original, rendered = original.strip(), rendered.strip()
        if not original or not rendered or original == rendered:
            continue
        if len(original) > _MAX_TERM_KEY_CHARS or len(rendered) > _MAX_TERM_VALUE_CHARS:
            continue
        if " ".join(original.split()) not in haystack:
            continue
        found.setdefault(original, rendered)
        if len(found) >= _MAX_TERMS_PER_CHUNK:
            break
    return found


# ---------- 接力包 ----------


def build_carryover(ctx: EpubContext, chapter_index: int, chunk_index: int) -> str:
    """拼出这一块的上下文接力包（块级 run 之间唯一的上下文通道）

    硬上限 CARRYOVER_MAX_TOKENS，超了按「接缝 > 术语 > 风格锚点」的优先级砍。
    这个包必须是常量级：一旦让它随块号增长，就又回到了被 400 撞墙的老路上。
    """
    chunks = ctx.chapter_chunks.get(chapter_index, [])
    stored = ctx.chunk_translations.get(chapter_index, {})
    if chunk_index >= len(chunks):
        return ""
    source = chunks[chunk_index]

    sections: List[str] = []  # 按优先级从高到低排列

    # 1. 接缝：上一块的原文尾 + 译文尾，让语气和未完的句子接得上
    if chunk_index > 0:
        prev_out = stored.get(chunk_index - 1, "")
        if prev_out.strip():
            prev_src = chunks[chunk_index - 1]
            sections.append(
                "## 上一块的结尾（仅供衔接参考，**不要重复翻译这部分**）\n"
                f"原文尾部：…{prev_src[-CARRYOVER_SEAM_CHARS:]}\n"
                f"译文尾部：…{prev_out[-CARRYOVER_SEAM_CHARS:]}\n"
            )

    # 2. 术语：只带 key 真的出现在当前块原文里的条目。
    #    灌全表会让提示词随书变长，而且模型看不见的术语等于噪音。
    terms = [(k, v) for k, v in ctx.glossary.items() if k and k in source]
    terms.sort(key=lambda kv: len(kv[0]), reverse=True)
    if terms:
        pairs = "\n".join(f"- {k} → {v}" for k, v in terms[:_MAX_CARRYOVER_TERMS])
        sections.append(
            "## 本块出现的专有名词（必须沿用这些译名）\n"
            f"{pairs}\n"
            "这些名字前文已经出现过，直接用译名即可，不要再在后面括号里写原文。\n"
        )

    # 3. 风格锚点：本章首块的原文/译文开头，防止长章节译到后面语气跑偏
    if chunk_index >= 2 and stored.get(0, "").strip():
        sections.append(
            "## 本章开头的译法（保持同样的语气与用词习惯）\n"
            f"原文：{chunks[0][:CARRYOVER_STYLE_CHARS]}…\n"
            f"译文：{stored[0][:CARRYOVER_STYLE_CHARS]}…\n"
        )

    kept: List[str] = []
    budget = CARRYOVER_MAX_TOKENS
    for text in sections:
        cost = EpubTools.count_tokens(text)
        if cost > budget:
            continue  # 砍掉这一节，继续试后面更小的
        budget -= cost
        kept.append(text)
    return "\n".join(kept)


def build_chunk_prompt(
    ctx: EpubContext, chapter_index: int, chunk_index: int, total: int, retry: bool
) -> str:
    """一块原文的完整用户提示词"""
    source = ctx.chapter_chunks[chapter_index][chunk_index]
    carryover = build_carryover(ctx, chapter_index, chunk_index)

    retry_note = ""
    if retry:
        # 重试时点名最常见的两个原因，比"再试一次"有效
        retry_note = (
            "\n上一次的译文被判定不合格（有整段内容没译到，或输出里混进了译文"
            "以外的东西）。请**完整**翻译下面的每一段，不要省略、不要概括，"
            "并且只输出译文本身。\n"
        )

    return f"""\
把下面这段 HTML 从 {ctx.source_language} 译成 {ctx.target_language}。

这是整章的第 {chunk_index + 1}/{total} 块。片段是从长文档里切出来的，
开头和结尾可能都在句子或标签中间，这是正常的，照原样译、照原样保留标签结构。
{carryover}{retry_note}
只输出译好的 HTML，不要加任何说明，不要用 ``` 围栏包裹。

{_SRC_BEGIN}
{source}
{_SRC_END}
"""


# ---------- 校验 ----------


def validate_chunk(
    source: str, translated: str, finish_reasons: Optional[List[str]] = None
) -> Tuple[bool, str]:
    """判定一块译文能不能收下。返回 (是否通过, 说明)

    刻意**不做**标签配平检查：EpubTools._atomize 会把开标签和闭标签拆成不同
    原子，原文块本身就可能是 `<section><p>…</p>` 这种半截片段，配平检查会把
    正确的译文全判死。
    漏译只看块级标签比例：块级标签与段落一一对应，少一个就是真的少一段；
    内联标签（脚注 <a>、<em>）模型会系统性吞掉而正文一字不缺，按全标签口径算
    会把译完的块误杀（实测《Marriage and Morals》第 5 章 49/63 就是这么卡死的）。
    """
    if not translated.strip():
        return False, "译文为空"

    leaked = [m for m in LEAKED_TOOL_CALL_MARKERS if m in translated]
    if leaked:
        # 块级 agent 没有任何工具，出现这些标记纯属模型自己编戏
        return False, f"输出里混进了文本形式的工具调用（{leaked[0]}）"

    if finish_reasons and any("length" in r for r in finish_reasons):
        # 输出被 max_tokens 截断，译文一定是半截的，不能收
        return False, "响应被 max_tokens 截断（finish_reason=length）"

    src_block = _count_tags(source, block_only=True)
    out_block = _count_tags(translated, block_only=True)
    if src_block and out_block < src_block * MIN_BLOCK_TAG_RATIO:
        missing = _describe_missing(
            {
                k: v
                for k, v in _missing_tag_names(source, translated).items()
                if k in _BLOCK_TAGS
            }
        )
        detail = f"，少了 {missing}" if missing else ""
        return False, f"块级标签 {out_block}/{src_block}，有整段没译到{detail}"

    src_tags = _count_tags(source)
    out_tags = _count_tags(translated)
    src_inline = src_tags - src_block
    out_inline = out_tags - out_block
    if src_inline and out_inline < src_inline * MIN_INLINE_TAG_RATIO:
        missing = _describe_missing(
            {
                k: v
                for k, v in _missing_tag_names(source, translated).items()
                if k not in _BLOCK_TAGS
            }
        )
        detail = f"，少了 {missing}" if missing else ""
        # 只提醒不拦：正文完整，重译一遍大概率还是同样吞标签，拦下来只烧重试次数
        return True, f"内联标签 {out_inline}/{src_inline}{detail}（不影响收下）"

    return True, ""


# ---------- 翻译 ----------


async def translate_one_chunk(
    agent: Agent[None, str],
    ctx: EpubContext,
    chapter_index: int,
    chunk_index: int,
    total: int,
) -> bool:
    """翻译一块，含缓存命中、透传、块级重试。返回这块是否通过校验"""
    logger = get_logger()
    source = ctx.chapter_chunks[chapter_index][chunk_index]
    stored = ctx.chunk_translations.setdefault(chapter_index, {})
    cache_id = ctx.chapters[chapter_index - 1].get_id()
    cacheable = bool(ctx.cache_manager and ctx.cache_key and cache_id)

    def _stats(translated: str, **extra) -> dict:
        return {
            "src_chars": len(source),
            "src_block_tags": _count_tags(source, block_only=True),
            "out_chars": len(translated),
            "out_block_tags": _count_tags(translated, block_only=True),
            **extra,
        }

    # 1. 块级缓存（内容哈希寻址）：命中就完全不发 API。
    #    这是块级续译的全部机制——文件存在即进度，不必为每块写一次进度文件。
    if cacheable:
        cached = ctx.cache_manager.load_chunk(ctx.cache_key, cache_id, source)  # type: ignore[union-attr]
        if cached and cached.strip():
            stored[chunk_index] = cached
            logger.chunk_result(
                chapter_index,
                chunk_index,
                total,
                0,
                _stats(cached, ok=True, cached=True),
            )
            return True

    # 2. 没有可译文本的块原样透传：省一次请求，也避开模型加戏
    if not has_translatable_text(source):
        stored[chunk_index] = source
        if cacheable:
            ctx.cache_manager.save_chunk(ctx.cache_key, cache_id, source, source)  # type: ignore[union-attr]
        logger.chunk_result(
            chapter_index,
            chunk_index,
            total,
            0,
            _stats(source, ok=True, passthrough=True),
        )
        return True

    # 3. 块级重试。attempts 跨章级重试累计，所以一个翻不动的坏块最多消耗
    #    MAX_CHUNK_RETRIES + 1 次请求，不会被章级重试乘出来。
    if ctx.attempts(chapter_index, chunk_index) > MAX_CHUNK_RETRIES:
        # 上一轮章级重试已经把这块的额度用光了。这里必须留一行，否则整块
        # "什么都没干就失败"在日志里是一片空白（红线 9）。
        logger.chunk_result(
            chapter_index,
            chunk_index,
            total,
            ctx.attempts(chapter_index, chunk_index),
            _stats(
                "",
                ok=False,
                reason=f"块级重试额度已用尽（{MAX_CHUNK_RETRIES + 1} 次），不再请求",
            ),
        )
        return False

    while ctx.attempts(chapter_index, chunk_index) <= MAX_CHUNK_RETRIES:
        attempt = ctx.record_attempt(chapter_index, chunk_index)
        prompt = build_chunk_prompt(
            ctx, chapter_index, chunk_index, total, retry=attempt > 1
        )

        try:
            # 流式 vs 非流式的取舍见 settings.STREAMING 注释：step_plan 上流式
            # 更易诱发推理跑飞（同块内容非流式尚有收敛样本、流式五次全部
            # 烧满预算零正文），当前默认非流式——TIMEOUT=360 的总量口径覆盖
            # 实测最长正常请求（200+ 秒）。
            if STREAMING:
                # 流式的超时语义是「等响应头 / 相邻 delta 的沉默 > TIMEOUT」，
                # 不是整请求总量；delta 逐个消费只为保活，全文以 get_output 为准。
                async with agent.run_stream(
                    prompt, usage_limits=UsageLimits(request_limit=2)
                ) as streamed:
                    async for _delta in streamed.stream_text(delta=True):
                        pass
                    raw = (await streamed.get_output()) or ""
                    result = streamed
            else:
                run_result = await agent.run(
                    prompt, usage_limits=UsageLimits(request_limit=2)
                )
                raw = run_result.output
                result = run_result
        except Exception as e:
            logger.chunk_result(
                chapter_index,
                chunk_index,
                total,
                attempt,
                _stats("", ok=False, reason=f"{type(e).__name__}: {e}"),
            )
            continue
        # 先剥壳（围栏 / 前言），再摘术语块，剩下的才是要写进书里的正文
        translated, terms_body = split_terms_block(clean_model_html(raw, source))
        terms = parse_terms_block(terms_body, source)
        # 截断判定只看最后一次响应：pydantic-ai 对 finish_reason=length 会自动
        # 补发一次请求（_agent_graph 的 ModelRetry），真正落进译文的是最后一条；
        # 被丢弃的截断响应不能连坐否决完整译文（2026-08-26 实测 length/stop 被误拒）
        reasons = logger.final_finish_reasons(result)
        ok, reason = validate_chunk(source, translated, reasons)

        # 通不过也把译文留在内存里：部分译文比原文有用，全章判定时按块级标签
        # 比例兜底。但**混进工具调用文本的输出一律不留**——这种输出的标签数
        # 往往是齐的，全章标签比例检查抓不住它，会把 <tool_call> 直接写进成品。
        # 丢掉之后这块变成"没有译文"，finalize_chapter 会拒绝保存整章，
        # 是一次如实的失败而不是静默污染。
        leaked = any(m in translated for m in LEAKED_TOOL_CALL_MARKERS)
        if translated.strip() and not leaked:
            stored[chunk_index] = translated

        logger.chunk_result(
            chapter_index,
            chunk_index,
            total,
            attempt,
            _stats(translated, ok=ok, reason=reason or None),
            result,
        )

        if ok:
            if cacheable:
                # 只有通过校验的块才进缓存：于是章级重试和 --resume 天然只重跑
                # 坏块，红线 5（残章不许标记完成）在块粒度上继续成立。
                ctx.cache_manager.save_chunk(  # type: ignore[union-attr]
                    ctx.cache_key, cache_id, source, translated
                )
            # 术语由模型在译文末尾的术语块里显式给出（键还要在原文里核对过），
            # 不依赖模型记得调 update_glossary。落盘走 update_progress。
            added = merge_glossary(ctx, terms)
            if added:
                logger.info(
                    f"章节 {chapter_index} 块 {chunk_index} 新增 {added} 个译名"
                )
            return True

        logger.dump_chunk(chapter_index, chunk_index, attempt, raw)

    return False


async def translate_chapter_chunks(
    agent: Agent[None, str], ctx: EpubContext, chapter_index: int, total: int
) -> Tuple[int, int]:
    """按块号顺序翻译整章。返回 (通过校验的块数, 总块数)

    顺序而不并发：接力包要用上一块的译文，而且 EpubContext 现在没有并发写保护。
    """
    logger = get_logger()
    passed = 0
    for chunk_index in range(total):
        if ctx.chunk_translations.get(chapter_index, {}).get(chunk_index, "").strip():
            # 已有译文（同一章内不该出现，防御性跳过）
            passed += 1
            continue
        if await translate_one_chunk(agent, ctx, chapter_index, chunk_index, total):
            passed += 1

    if passed < total:
        logger.info(f"章节 {chapter_index} 有 {total - passed}/{total} 块未通过校验")
    return passed, total
