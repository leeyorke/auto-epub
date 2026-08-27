"""Tests for translator.py - 编排层（不发 API 的辅助逻辑与状态流转）。"""

from unittest.mock import Mock, patch

from ebooklib import epub

from auto_epub.agent_tools import EpubContext
from auto_epub.models import TranslationProgress
from auto_epub.translator import EpubTranslator


def make_translator(cache_manager=None):
    t = EpubTranslator(agent=Mock(), cache_enabled=False)
    t.cache_manager = cache_manager
    return t


def make_context_with_chapters(chapter_ids):
    book = epub.EpubBook()
    chapters = []
    for i, cid in enumerate(chapter_ids, 1):
        ch = epub.EpubHtml(title=f"c{i}", file_name=cid, lang="en")
        ch.content = f"<html><body><p>chapter {i}</p></body></html>".encode()
        book.add_item(ch)
        chapters.append(ch)
    return EpubContext(
        book=book,
        target_language="zh",
        cache_key=None,
        cache_manager=None,
        glossary={},
    )


class TestInit:
    def test_cache_enabled_false_skips_cache_manager(self):
        t = EpubTranslator(agent=Mock(), cache_enabled=False)
        assert t.cache_manager is None

    def test_chunk_agent_defaults_to_none(self):
        t = EpubTranslator(agent=Mock(), cache_enabled=False)
        assert t.chunk_agent is None

    def test_lazy_chunk_agent_creation(self):
        """chunk_agent 为 None 时按目标语言现建（缓存复用，只建一次）。"""
        t = EpubTranslator(agent=Mock(), cache_enabled=False)
        fake_agent = object()
        with patch(
            "auto_epub.client.create_chunk_agent", return_value=fake_agent
        ) as factory:
            first = t._resolve_chunk_agent("zh")
            second = t._resolve_chunk_agent("en")  # 已建过就不再新建
        assert first is fake_agent and second is fake_agent
        factory.assert_called_once_with("zh")


class TestPendingChapters:
    def test_excludes_completed(self):
        t = make_translator()
        ctx = make_context_with_chapters(["a.xhtml", "b.xhtml", "c.xhtml"])
        # 匹配键是 chapter.get_id()（内存对象缺省为 chapter_N）
        ids = [c.get_id() for c in ctx.chapters]
        progress = TranslationProgress(
            source_lang="en",
            target_lang="zh",
            total_chapters=3,
            completed_chapters=[ids[1]],
        )
        pending = t._pending_chapters(ctx, progress)
        assert [c.get_name() for _, c in pending] == ["a.xhtml", "c.xhtml"]
        # 索引从 1 开始
        assert [i for i, _ in pending] == [1, 3]

    def test_all_done_returns_empty(self):
        ctx = make_context_with_chapters(["only.xhtml"])
        progress = TranslationProgress(
            source_lang="en",
            target_lang="zh",
            total_chapters=1,
            completed_chapters=[ctx.chapters[0].get_id()],
        )
        assert make_translator()._pending_chapters(ctx, progress) == []


class TestGenerateOutputPath:
    def test_appends_language_before_suffix(self):
        t = make_translator()
        out = t._generate_output_path(r"C:\books\Story.epub", "zh")
        assert out.endswith("Story(zh).epub")

    def test_keeps_directory(self):
        t = make_translator()
        out = t._generate_output_path(r"C:\books\Story.epub", "ja")
        assert out.startswith(r"C:\books")


class TestMarkFailed:
    def test_appends_once_via_update_progress(self):
        t = make_translator(cache_manager=Mock())
        ctx = EpubContext(
            book=epub.EpubBook(),
            target_language="zh",
            cache_key="k",
            cache_manager=t.cache_manager,
            glossary={},
        )
        t._mark_failed(ctx, "ch9")
        mark = t.cache_manager.update_progress.call_args[0][1]
        stub = TranslationProgress(source_lang="en", target_lang="zh", total_chapters=1)
        mark(stub)
        assert stub.failed_chapters == ["ch9"]
        # 幂等：重复标记不重复记录
        mark(stub)
        assert stub.failed_chapters.count("ch9") == 1

    def test_noop_without_cache(self):
        t = make_translator(None)
        ctx = EpubContext(
            book=epub.EpubBook(),
            target_language="zh",
            cache_key=None,
            cache_manager=None,
            glossary={},
        )
        t._mark_failed(ctx, "ch9")  # 不应抛错


class TestRestoreCachedChapters:
    def _setup(self, cached_content="<p>cached</p>"):
        t = make_translator(cache_manager=Mock())
        t.logger = Mock()
        ctx = make_context_with_chapters(["a.xhtml", "b.xhtml"])
        ids = [c.get_id() for c in ctx.chapters]
        progress = TranslationProgress(
            source_lang="en",
            target_lang="zh",
            total_chapters=2,
            completed_chapters=list(ids),
        )
        t.cache_manager.load_chapter.side_effect = lambda key, cid: (
            cached_content if cid == ids[0] else None
        )
        return t, ctx, progress

    def test_restores_cached_and_kicks_out_missing(self):
        """进度说已完成但译文文件丢失 → 踢回待翻译并立即落盘修正。"""
        t, ctx, progress = self._setup()
        t._restore_cached_chapters(ctx, progress, "key")

        a_raw = ctx.chapters[0].get_content().decode("utf-8", errors="ignore")
        assert "cached" in a_raw
        # 缓存缺失的章节：从 completed 移除，--resume 才会重译而不是输出原文
        assert progress.completed_chapters == [ctx.chapters[0].get_id()]
        t.cache_manager.save_progress.assert_called_once()

    def test_disabled_cache_is_noop(self):
        t = make_translator(None)
        t.logger = Mock()
        ctx = make_context_with_chapters(["a.xhtml"])
        progress = TranslationProgress(
            source_lang="en",
            target_lang="zh",
            total_chapters=1,
            completed_chapters=["a.xhtml"],
        )
        t._restore_cached_chapters(ctx, progress, "k")  # 不抛错即通过
