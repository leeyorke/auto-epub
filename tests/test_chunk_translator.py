"""Tests for chunk_translator.py - 块级翻译核心逻辑。

纯函数与内存态 EpubContext 覆盖：输出清理、术语块、接力包、校验器，
以及 translate_one_chunk 的缓存命中 / 无文本透传两条分支（不发 API）。
"""

import asyncio
from unittest.mock import Mock

from ebooklib import epub

import auto_epub.chunk_translator as ct
from auto_epub.agent_tools import EpubContext
from auto_epub.chunk_translator import (
    _SRC_BEGIN,
    _SRC_END,
    TERMS_BLOCK_BEGIN,
    TERMS_BLOCK_END,
    build_chunk_prompt,
    build_carryover,
    clean_model_html,
    has_translatable_text,
    parse_terms_block,
    split_terms_block,
    translate_one_chunk,
    validate_chunk,
)


def make_context(chunks_by_chapter=None):
    """构建一个内存态 EpubContext（book 只有元数据，分块状态手工注入）。"""
    book = epub.EpubBook()
    ch = epub.EpubHtml(title="c", file_name="ch01.xhtml", lang="en")
    ch.content = b"<html><body><p>x</p></body></html>"
    book.add_item(ch)
    ctx = EpubContext(
        book=book,
        target_language="zh",
        cache_key="k",
        cache_manager=None,
        glossary={},
    )
    for idx, chunks in (chunks_by_chapter or {}).items():
        ctx.chapter_chunks[idx] = chunks
        ctx.chunk_translations[idx] = {}
    return ctx


# ---------------------------------------------------------------------------
# clean_model_html
# ---------------------------------------------------------------------------


class TestCleanModelHtml:
    def test_plain_passthrough(self):
        assert clean_model_html("<p>Hello</p>") == "<p>Hello</p>"

    def test_strips_markdown_fence(self):
        raw = "```html\n<p>你好</p>\n```"
        assert clean_model_html(raw) == "<p>你好</p>"

    def test_strips_bare_fence_language(self):
        raw = "```\n<p>你好</p>\n```"
        assert clean_model_html(raw) == "<p>你好</p>"

    def test_unclosed_fence_takes_body_after_first_line(self):
        raw = "```html\n<p>你好</p>"
        assert clean_model_html(raw) == "<p>你好</p>"

    def test_unclosed_fence_single_line_is_empty(self):
        # 只有开围栏且没有换行：无法取出正文，宁可清空也不能把 ``` 写进书里
        assert clean_model_html("```html") == ""

    def test_strips_preamble_before_first_tag(self):
        raw = "以下是译文：\n<p>你好</p>"
        assert clean_model_html(raw, source="<p>Hello</p>") == "<p>你好</p>"

    def test_no_preamble_strip_for_plain_source(self):
        # 原文本身没有标签时不能按 "<" 截断，否则整块译文会被清空
        raw = "Plain translation."
        assert clean_model_html(raw, source="Plain source") == "Plain translation."

    def test_removes_source_boundary_markers(self):
        raw = f"{_SRC_BEGIN}\n<p>x</p>\n{_SRC_END}"
        assert clean_model_html(raw) == "<p>x</p>"

    def test_empty_and_none(self):
        assert clean_model_html("") == ""
        assert clean_model_html(None) == ""


# ---------------------------------------------------------------------------
# has_translatable_text
# ---------------------------------------------------------------------------


class TestHasTranslatableText:
    def test_closing_tags_only(self):
        assert has_translatable_text("</section></div></body>") is False

    def test_numbers_only(self):
        assert has_translatable_text("<td>123</td><td>45</td>") is False

    def test_entities_only(self):
        assert has_translatable_text("<p>&#160;&amp;</p>") is False

    def test_letters_present(self):
        assert has_translatable_text("<p>Hello world</p>") is True

    def test_any_alphabet_counts(self):
        # 目标语言任意，isalpha 不限语种
        assert has_translatable_text("<p>Привет</p>") is True


# ---------------------------------------------------------------------------
# split_terms_block / parse_terms_block
# ---------------------------------------------------------------------------


class TestSplitTermsBlock:
    def test_no_terms_block_passthrough(self):
        body, terms = split_terms_block("<p>正文</p>")
        assert body == "<p>正文</p>"
        assert terms == ""

    def test_complete_block_removed(self):
        translated = f"<p>正文</p>{TERMS_BLOCK_BEGIN}\nA=B\n{TERMS_BLOCK_END}"
        body, terms = split_terms_block(translated)
        assert body == "<p>正文</p>"
        assert terms == "\nA=B\n"

    def test_missing_end_marker_falls_back_to_tail_cut(self):
        # 漏写结束标记时从开始标记切到末尾，宁多勿少
        translated = "<p>正文</p><!--TERMS\nA=B"
        body, terms = split_terms_block(translated)
        assert body == "<p>正文</p>"
        assert terms == "\nA=B"


class TestParseTermsBlock:
    def test_accepts_lines_with_key_in_source(self):
        src = "<p>Julien Sorel met Madame de Renal.</p>"
        found = parse_terms_block("Julien Sorel=于连·索雷尔", src)
        assert found == {"Julien Sorel": "于连·索雷尔"}

    def test_key_normalized_whitespace_matches_multiline_source(self):
        # EPUB 原文里跨行断词很常见："Madame de\n    Renal"
        src = "<p>Madame de\n    Renal came.</p>"
        found = parse_terms_block("Madame de Renal=德·雷纳夫人", src)
        assert found == {"Madame de Renal": "德·雷纳夫人"}

    def test_rejects_hallucinated_keys(self):
        # 键没出现在原文里：模型幻觉，必须挡掉
        found = parse_terms_block("Imaginary Name=幻想名", "<p>No such name.</p>")
        assert found == {}

    def test_identical_translation_skipped(self):
        found = parse_terms_block("Paris=Paris", "<p>Paris.</p>")
        assert found == {}

    def test_dash_prefix_stripped(self):
        found = parse_terms_block("- Julien=于连", "<p>Julien.</p>")
        assert found == {"Julien": "于连"}

    def test_line_without_equals_skipped(self):
        found = parse_terms_block("just some note", "<p>note</p>")
        assert found == {}

    def test_length_limits(self):
        long_key = "K" * 65
        found = parse_terms_block(f"{long_key}=译名", "<p>" + "K" * 70 + "</p>")
        assert found == {}


# ---------------------------------------------------------------------------
# build_carryover / build_chunk_prompt
# ---------------------------------------------------------------------------


class TestBuildCarryover:
    def test_out_of_range_returns_empty(self):
        ctx = make_context({1: ["<p>a</p>"]})
        assert build_carryover(ctx, 1, 5) == ""

    def test_first_chunk_has_only_matching_terms(self):
        ctx = make_context({1: ["<p>Julien walks</p><p>more</p>"]})
        ctx.glossary = {"Julien": "于连", "Unseen": "没见过"}
        text = build_carryover(ctx, 1, 0)
        # 术语：只带出现在当前块里的键
        assert "Julien" in text and "于连" in text
        assert "Unseen" not in text
        # 第一块没有接缝、没有风格锚点
        assert "上一块的结尾" not in text
        assert "本章开头的译法" not in text

    def test_second_chunk_carries_seam(self):
        ctx = make_context({1: ["<p>prev</p>", "<p>cur</p>"]})
        ctx.chunk_translations[1][0] = "<p>上块译文</p>"
        text = build_carryover(ctx, 1, 1)
        assert "上一块的结尾" in text
        assert "上块译文" in text

    def test_style_anchor_from_third_chunk_onward(self):
        chunks = ["<p>first</p>", "<p>second</p>", "<p>third</p>"]
        ctx = make_context({1: chunks})
        ctx.chunk_translations[1][0] = "<p>首块译文</p>"
        ctx.chunk_translations[1][1] = "<p>第二块译文</p>"
        assert "本章开头的译法" not in build_carryover(ctx, 1, 1)
        assert "本章开头的译法" in build_carryover(ctx, 1, 2)

    def test_budget_cut_drops_everything_when_tiny(self, monkeypatch):
        # 接力包硬上限：预算为 1 时任何一节都装不下，全部砍掉
        ctx = make_context({1: ["<p>a</p>", "<p>b</p>"]})
        ctx.chunk_translations[1][0] = "<p>译文</p>"
        monkeypatch.setattr(ct, "CARRYOVER_MAX_TOKENS", 1)
        assert build_carryover(ctx, 1, 1) == ""


class TestBuildChunkPrompt:
    def _ctx(self):
        ctx = make_context({1: ["<p>chunk one</p>", "<p>chunk two</p>"]})
        return ctx

    def test_contains_source_between_markers(self):
        prompt = build_chunk_prompt(self._ctx(), 1, 0, 2, retry=False)
        idx_a = prompt.find(_SRC_BEGIN)
        idx_b = prompt.find(_SRC_END)
        assert 0 < idx_a < idx_b
        assert "<p>chunk one</p>" in prompt[idx_a:idx_b]
        # 边界标记故意不以 "<" 开头，防止剥前言时混进译文
        assert not _SRC_BEGIN.startswith("<")

    def test_mentions_target_language_and_position(self):
        prompt = build_chunk_prompt(self._ctx(), 1, 0, 2, retry=False)
        assert "zh" in prompt
        assert "第 1/2 块" in prompt

    def test_retry_adds_strict_note(self):
        normal = build_chunk_prompt(self._ctx(), 1, 0, 2, retry=False)
        retried = build_chunk_prompt(self._ctx(), 1, 0, 2, retry=True)
        assert "判定不合格" in retried and "判定不合格" not in normal


# ---------------------------------------------------------------------------
# validate_chunk
# ---------------------------------------------------------------------------


class TestValidateChunk:
    def test_empty_translation_rejected(self):
        ok, reason = validate_chunk("<p>src</p>", "   ")
        assert ok is False and reason == "译文为空"

    def test_leaked_tool_call_rejected(self):
        ok, reason = validate_chunk("<p>s</p>", "<p>t</p>\n<tool_call>{}</tool_call>")
        assert ok is False and "工具调用" in reason

    def test_truncated_finish_reason_rejected(self):
        ok, reason = validate_chunk("<p>s</p>", "<p>t</p>", ["length"])
        assert ok is False and "截断" in reason

    def test_balance_passes_even_with_unbalanced_tags(self):
        """刻意不做标签配平检查：半截片段是正常的。"""
        ok, reason = validate_chunk("<section><p>src</p>", "<section><p>译</p>")
        assert ok is True and reason == ""

    def test_missing_block_tags_rejected(self):
        source = "".join(f"<p>Sentence {i} words.</p>" for i in range(10))
        translated = "<p>只有一段。</p>"
        ok, reason = validate_chunk(source, translated)
        assert ok is False
        assert "块级标签" in reason

    def test_inline_tag_loss_warns_but_passes(self):
        """内联标签吞掉不阻塞收下，只在说明里点名。"""
        source = (
            "<p>One <a href='#x'>link</a>.</p>"
            "<p>Two <a href='#y'>link</a>.</p>"
            "<p>Three <a href='#z'>link</a>.</p>"
        )
        translated = "<p>一。</p><p>二。</p><p>三。</p>"  # 3 个 <a> 全吞
        ok, reason = validate_chunk(source, translated)
        assert ok is True
        assert "内联标签" in reason

    def test_partial_inline_loss_within_ratio_passes_silently(self):
        source = "<p>A <em>x</em></p><p>B <em>y</em></p><p>C <em>z</em></p>"
        lost_one = "<p>甲 <em>x</em></p><p>乙 <em>y</em></p><p>丙</p>"
        # 丢了 1/3 内联 <em>：(2/3)=0.67 < 0.8 会告警；这里验证告警路径不拦截
        ok, reason = validate_chunk(source, lost_one)
        assert ok is True

    def test_img_counts_as_block_tag(self):
        source = "<p>x</p><img src='a.png'/>"
        translated = "<p>y</p>"
        ok, reason = validate_chunk(source, translated)
        assert ok is False  # 丢图也是内容缺失，比例 1/2 < 0.8


# ---------------------------------------------------------------------------
# translate_one_chunk 的两条零 API 分支
# ---------------------------------------------------------------------------


def _context_for_translate(cache_manager=None):
    book = epub.EpubBook()
    ch = epub.EpubHtml(title="c", file_name="ch01.xhtml", lang="en")
    ch.content = b"<html><body><p>x</p></body></html>"
    book.add_item(ch)
    ctx = EpubContext(
        book=book,
        target_language="zh",
        cache_key="ck",
        cache_manager=cache_manager,
        glossary={},
    )
    ctx.chapter_chunks[1] = []
    ctx.chunk_translations[1] = {}
    return ctx


class TestTranslateOneChunkNoApiBranches:
    def test_cache_hit_never_calls_agent(self):
        cache = Mock()
        cache.load_chunk.return_value = "<p>缓存译文</p>"
        ctx = _context_for_translate(cache_manager=cache)
        ctx.chapter_chunks[1] = ["<p>Hello</p>"]
        agent = Mock()
        agent.run = Mock(side_effect=AssertionError("不应发请求"))

        done = asyncio.run(translate_one_chunk(agent, ctx, 1, 0, 1))
        assert done is True
        assert ctx.chunk_translations[1][0] == "<p>缓存译文</p>"
        agent.run.assert_not_called()

    def test_non_translatable_chunk_passthrough_without_request(self):
        ctx = _context_for_translate()
        ctx.chapter_chunks[1] = ["</section></div>"]
        agent = Mock()
        agent.run = Mock(side_effect=AssertionError("不应发请求"))

        done = asyncio.run(translate_one_chunk(agent, ctx, 1, 0, 1))
        assert done is True
        assert ctx.chunk_translations[1][0] == "</section></div>"
        agent.run.assert_not_called()

    def test_exhausted_retry_budget_logs_and_fails_without_request(self):
        ctx = _context_for_translate()
        ctx.chapter_chunks[1] = ["<p>Hello</p>"]
        # 跨章级重试累计的额度已用尽（红线 8）
        for _ in range(ct.MAX_CHUNK_RETRIES + 1):
            ctx.record_attempt(1, 0)
        agent = Mock()
        agent.run = Mock(side_effect=AssertionError("额度用尽后不应再请求"))

        done = asyncio.run(translate_one_chunk(agent, ctx, 1, 0, 1))
        assert done is False
        agent.run.assert_not_called()
