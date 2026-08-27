"""Tests for agent_tools.py - 工具层纯函数与 EpubContext 状态机。

不触发任何文件系统写入：cache_manager 一律传 Mock 或 None。
"""

from unittest.mock import Mock

from ebooklib import epub

from auto_epub.agent_tools import (
    EpubContext,
    _count_tags,
    _describe_missing,
    _missing_tag_names,
    apply_toc_titles,
    collect_toc_titles,
    finalize_chapter,
    merge_glossary,
)


def make_context():
    book = epub.EpubBook()
    ch = epub.EpubHtml(title="c", file_name="ch01.xhtml", lang="en")
    ch.content = b"<html><head><link rel='stylesheet' href='style.css'/></head><body><p>x</p></body></html>"
    book.add_item(ch)
    return EpubContext(
        book=book,
        target_language="zh",
        cache_key="k",
        cache_manager=None,
        glossary={},
    )


# ---------------------------------------------------------------------------
# 标签计数（漏译判定的口径基础）
# ---------------------------------------------------------------------------


class TestTagCounting:
    def test_counts_open_and_close_tags(self):
        assert _count_tags("<p>a</p><b>c</b>") == 4

    def test_ignores_comments_and_xml_decl(self):
        # 注释与 <?xml 不算标签
        html = '<?xml version="1.0"?><!-- comment --><p>x</p>'
        assert _count_tags(html) == 2

    def test_block_only_counts_block_tags(self):
        html = "<div><p>1 <a href='#'>l</a></p><em>e</em></div>"
        # 全部标签：div+/div+p+/p+a+/a+em+/em
        assert _count_tags(html) == 8
        # 块级只数 div 与 p（名称口径，含开闭）
        assert _count_tags(html, block_only=True) == 4

    def test_img_is_block_level(self):
        assert _count_tags("<img src='x'/>", block_only=True) == 1


class TestMissingTags:
    def test_reports_opening_tag_deficit_ordered_by_count(self):
        source = "<p>1</p><p>2</p><p>3</p><a>x</a><em>y</em>"
        translated = "<p>1</p><p>2</p>"
        missing = _missing_tag_names(source, translated)
        assert missing == {"p": 1, "a": 1, "em": 1}

    def test_describe_format(self):
        text = _describe_missing({"a": 5, "em": 2})
        assert text == "5 个 <a>、2 个 <em>"


# ---------------------------------------------------------------------------
# 目录结构同序往返（collect ↔ apply）
# ---------------------------------------------------------------------------


def sample_toc():
    child = epub.Link("ch2.xhtml", "Chapter Two", "c2")
    parent = epub.Link("ch1.xhtml", "Chapter One", "c1")
    return [parent, (epub.Link("s.xhtml", "Section", "s"), [child])]


class TestTocCollectApply:
    def test_collect_flattens_in_recursive_order(self):
        titles = collect_toc_titles(sample_toc())
        assert titles == ["Chapter One", "Section", "Chapter Two"]

    def test_apply_roundtrip_preserves_order_and_fields(self):
        toc = sample_toc()
        new_titles = ["第一章", "第一节", "第二章"]
        rebuilt = apply_toc_titles(toc, iter(new_titles))
        assert collect_toc_titles(rebuilt) == new_titles

        top_link = rebuilt[0]
        assert top_link.href == "ch1.xhtml" and top_link.uid == "c1"
        section, children = rebuilt[1]
        assert children[0].href == "ch2.xhtml"

    def test_apply_with_fewer_titles_keeps_original_tail(self):
        toc = sample_toc()
        rebuilt = apply_toc_titles(toc, iter(["只有一条"]))
        assert collect_toc_titles(rebuilt)[0] == "只有一条"
        # 缺失的条目保持原标题而不是报错
        assert len(collect_toc_titles(rebuilt)) == 3


# ---------------------------------------------------------------------------
# EpubContext 状态机
# ---------------------------------------------------------------------------


class TestEpubContextState:
    def test_pending_chunks_tracks_missing_translations(self):
        ctx = make_context()
        ctx.chapter_chunks[1] = ["<p>a</p>", "<p>b</p>", "<p>c</p>"]
        ctx.chunk_translations[1] = {0: "<p>甲</p>", 2: ""}
        # 块 1 完全没有译文、块 2 是空串——都算待译
        assert ctx.pending_chunks(1) == [1, 2]

    def test_assembled_translation_sorts_by_chunk_index(self):
        """乱序写入也要能按块号还原正确顺序。"""
        ctx = make_context()
        ctx.chapter_chunks[1] = ["A", "B", "C"]
        ctx.chunk_translations[1] = {2: "丙", 0: "甲", 1: "乙"}
        assert ctx.assembled_translation(1) == "甲乙丙"

    def test_thin_chunks_detects_block_tag_shortfall_only(self):
        ctx = make_context()
        ctx.chapter_chunks[1] = ["<p>one</p><p>two</p>", "<p>x <em>i</em></p>"]
        ctx.chunk_translations[1] = {
            0: "<p>一。</p>",  # 块级 1/2 = 0.5 → thin
            1: "<p>x</p>",  # 内联 em 被吞，块级 1/1 → 不算 thin
        }
        assert ctx.thin_chunks(1) == [0]

    def test_prepare_chapter_does_not_reset_attempts(self):
        """红线 8：chunk_attempts 绝不能在 prepare_chapter 里重置。"""
        ctx = make_context()
        ctx.record_attempt(1, 0)
        ctx.prepare_chapter(1)
        assert ctx.attempts(1, 0) == 1

    def test_reset_chapter_keeps_attempts(self):
        ctx = make_context()
        ctx.record_attempt(1, 0)
        ctx.reset_chapter(1)
        assert ctx.attempts(1, 0) == 1
        assert ctx.chunk_count(1) == 0

    def test_record_attempt_accumulates(self):
        ctx = make_context()
        assert ctx.record_attempt(1, 0) == 1
        assert ctx.record_attempt(1, 0) == 2
        assert ctx.attempts(1, 0) == 2


# ---------------------------------------------------------------------------
# merge_glossary（先出现的译名说了算）
# ---------------------------------------------------------------------------


class TestMergeGlossary:
    def test_new_terms_merged_and_counted(self):
        ctx = make_context()
        added = merge_glossary(ctx, {"Julien": "于连"})
        assert added == 1
        assert ctx.glossary["Julien"] == "于连"

    def test_existing_keys_not_overwritten(self):
        ctx = make_context()
        ctx.glossary["Julien"] = "于连"
        added = merge_glossary(ctx, {"Julien": "另一个译名"})
        assert added == 0
        assert ctx.glossary["Julien"] == "于连"

    def test_empty_or_blank_entries_skipped(self):
        ctx = make_context()
        assert merge_glossary(ctx, {"": "v", "k": ""}) == 0

    def test_persists_via_update_progress_when_cache_enabled(self):
        ctx = make_context()
        ctx.cache_manager = Mock()
        merge_glossary(ctx, {"Renal": "雷纳尔"})
        ctx.cache_manager.update_progress.assert_called_once()
        args = ctx.cache_manager.update_progress.call_args
        progress = TranslationProgressStub()
        args.args[1](progress) if args.args else args[0][1](progress)
        assert progress.glossary == {"Renal": "雷纳尔"}

    def test_no_cache_write_without_cache_key(self):
        ctx = make_context()
        ctx.cache_manager = Mock()
        ctx.cache_key = None
        merge_glossary(ctx, {"a": "b"})
        ctx.cache_manager.update_progress.assert_not_called()


class TranslationProgressStub:
    def __init__(self):
        self.glossary = {}
        self.completed_chapters = []
        self.failed_chapters = []
        self.toc_translated = False
        self.toc_titles = []
        self.images_translated = {}


# ---------------------------------------------------------------------------
# finalize_chapter：保存闸门
# ---------------------------------------------------------------------------


class TestFinalizeChapter:
    def _filled_context(self, cache_manager=None):
        ctx = make_context()
        if cache_manager:
            ctx.cache_manager = cache_manager
        chunks = [
            "<p>First paragraph.</p>",
            "<p>Second paragraph.</p>",
        ]
        translations = {
            i: c.replace("First", "第一").replace("Second", "第二")
            for i, c in enumerate(chunks)
        }
        ctx.chapter_chunks[1] = chunks
        ctx.chunk_translations[1] = translations
        return ctx

    def test_refuses_when_any_chunk_untranslated(self):
        """红线 5：有块没译文就不许保存。"""
        cache = Mock()
        ctx = self._filled_context(cache)
        del ctx.chunk_translations[1][1]

        ok, reason = finalize_chapter(ctx, 1)
        assert ok is False
        assert "没有译文" in reason
        # 拒绝时绝不写进度（completed_chapters 一个都不会加）
        cache.update_progress.assert_not_called()

    def test_complete_translation_marks_saved_and_done(self):
        cache = Mock()
        ctx = self._filled_context(cache)

        ok, reason = finalize_chapter(ctx, 1)
        assert ok is True
        assert 1 in ctx.saved_chapters
        cache.save_chapter.assert_called_once()
        # 完整保存才进 completed_chapters（否则 --resume 永远跳过残章）
        mark = cache.update_progress.call_args[0][1]
        stub = TranslationProgressStub()
        mark(stub)
        # get_id() 的缺省实现返回 chapter_0；用真实值核对而不硬编码
        assert ctx.chapters[0].get_id() in stub.completed_chapters

    def test_incomplete_still_writes_book_but_not_completed(self):
        """【不变量】判定不完整的章节照写 book（部分译文有用），但不标完成。"""
        cache = Mock()
        ctx = self._filled_context(cache)
        # 每块都有译文、但第 2 块完全没有块级标签：全章比例 1/2 < 0.8
        ctx.chunk_translations[1] = {
            0: "<p>第一段译好了。</p>",
            1: "<span>第二段只有内联。</span>",
        }

        ok, reason = finalize_chapter(ctx, 1)
        assert ok is False
        assert "块级标签" in reason
        assert 1 in ctx.incomplete_chapters
        # 仍然写进了 book 与章节缓存（部分译文比原文有用），
        # 但不进 saved/completed（红线 5，否则 --resume 永远跳过残章）
        assert 1 not in ctx.saved_chapters
        cache.save_chapter.assert_called_once()
        cache.update_progress.assert_not_called()

    def test_registers_stylesheet_from_raw_bytes(self):
        """ebooklib 写盘用自家模板重建 head，样式表必须先 add_link 注册回去。"""
        cache = Mock()
        ctx = self._filled_context(cache)
        finalize_chapter(ctx, 1)
        links = [(lnk.get("href"), lnk.get("rel")) for lnk in ctx.chapters[0].links]
        assert ("style.css", "stylesheet") in links

    def test_zero_chunks_rejected(self):
        ctx = make_context()
        ok, reason = finalize_chapter(ctx, 9)
        assert ok is False and "没有分块内容" in reason

    def test_reason_mentions_block_tag_ratio_on_success(self):
        ctx = self._filled_context()
        ok, reason = finalize_chapter(ctx, 1)
        assert ok is True and "块级标签" in reason
