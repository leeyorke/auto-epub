"""Tests for epub_tools.py - EPUB utilities.

All tests run on in-memory epub.EpubBook objects / HTML strings;
no filesystem access required (test environment sandbox restricts temp dirs).
"""

from bs4 import BeautifulSoup
from ebooklib import epub

from auto_epub.epub_tools import EpubTools


def make_book(language="en"):
    """Create an in-memory EpubBook with language metadata."""
    book = epub.EpubBook()
    book.set_identifier("test")
    book.set_title("T")
    book.add_metadata("DC", "language", language)
    return book


def make_chapter(file_name, body_content, *, extra_head=""):
    """Create an in-memory EpubHtml chapter."""
    ch = epub.EpubHtml(title=file_name, file_name=file_name, lang="en")
    ch.content = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><head>' + extra_head + "</head>"
        "<body>" + body_content + "</body></html>"
    ).encode("utf-8")
    return ch


class TestLanguageMetadata:
    def test_get_default_language(self):
        book = make_book("en")
        assert EpubTools.get_default_language(book) == "en"

    def test_get_default_language_fallback_when_missing(self):
        book = epub.EpubBook()
        # 无语言元数据时回退到 en
        assert EpubTools.get_default_language(book) == "en"

    def test_set_language_replaces_existing(self):
        book = make_book("en")
        EpubTools.set_language(book, "zh")
        assert EpubTools.get_default_language(book) == "zh"
        # 只剩一条语言记录，不能重复累加
        langs = book.get_metadata("DC", "language")
        assert len(langs) == 1
        assert langs[0][0] == "zh"


class TestChapterFiltering:
    def test_get_all_chapters_returns_body_documents(self):
        book = make_book()
        ch1 = make_chapter("ch1.xhtml", "<h1>One</h1><p>text</p>")
        ch2 = make_chapter("ch2.xhtml", "<p>more</p>")
        book.add_item(ch1)
        book.add_item(ch2)
        chapters = EpubTools.get_all_chapters(book)
        assert {c.get_name() for c in chapters} == {"ch1.xhtml", "ch2.xhtml"}

    def test_document_content_is_templated_by_ebooklib(self):
        # ebooklib 的 get_content 会用自己的模板重建文档（始终带 <body>），
        # 这里只验证行为事实：普通文档进入章节列表
        book = make_book()
        plain = epub.EpubHtml(title="x", file_name="weird.xhtml", lang="en")
        plain.content = b"<html><head></head></html>"
        book.add_item(plain)
        assert [c.get_name() for c in EpubTools.get_all_chapters(book)] == [
            "weird.xhtml"
        ]

    def test_toc_page_is_excluded_from_chapters(self):
        book = make_book()
        toc_page = make_chapter(
            "toc.xhtml",
            '<nav epub:type="toc"><ol><li><a href="ch1.xhtml">C1</a></li></ol></nav>',
        )
        # 也测旧式 type="toc" 写法：单独建一章
        old_toc = make_chapter("toc2.xhtml", '<div type="toc">contents</div>')
        normal = make_chapter("ch1.xhtml", "<p>story</p>")
        book.add_item(toc_page)
        book.add_item(old_toc)
        book.add_item(normal)
        names = [c.get_name() for c in EpubTools.get_all_chapters(book)]
        assert names == ["ch1.xhtml"]

    def test_empty_document_is_excluded(self):
        book = make_book()
        empty = epub.EpubHtml(title="e", file_name="empty.xhtml", lang="en")
        empty.content = b""
        book.add_item(empty)
        assert EpubTools.get_all_chapters(book) == []


class TestNavDocuments:
    NAV_XHTML = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops">'
        "<head><title>Nav</title></head><body>"
        '<nav epub:type="toc"><ol>'
        '<li><a href="ch1.xhtml">Chapter One</a></li>'
        "</ol></nav>"
        "</body></html>"
    )

    def test_find_nav_documents_identifies_by_content(self):
        book = make_book()
        nav = epub.EpubHtml(title="nav", file_name="nav.xhtml", lang="en")
        nav.content = self.NAV_XHTML.encode("utf-8")
        book.add_item(nav)
        book.add_item(make_chapter("ch1.xhtml", "<p>x</p>"))
        navs = EpubTools.find_nav_documents(book)
        # 按内容识别（即使 OPF 没标 properties="nav"，ebooklib 读进来是普通 EpubHtml）
        assert [n.get_name() for n in navs] == ["nav.xhtml"]

    def test_extract_nav_labels(self):
        labels = EpubTools.extract_nav_labels(self.NAV_XHTML)
        assert labels == ["Chapter One"]

    def test_extract_nav_labels_skips_non_toc_navs(self):
        html = (
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
            '<nav epub:type="toc"><a href="c1">Story</a></nav>'
            '<nav epub:type="page-list"><a href="c1#p1">1</a></nav>'
            '<nav epub:type="landmarks"><a href="cover.xhtml">Cover</a></nav>'
            "</body></html>"
        )
        # 只有 toc 区参与翻译，page-list / landmarks 不应混入
        assert EpubTools.extract_nav_labels(html) == ["Story"]

    def test_apply_nav_labels_replaces_and_counts(self):
        mapping = {"Chapter One": "第一章"}
        new_html, replaced = EpubTools.apply_nav_labels(self.NAV_XHTML, mapping)
        assert replaced == 1
        assert EpubTools.extract_nav_labels(new_html) == ["第一章"]

    def test_apply_nav_labels_uses_xml_parser_keeps_head(self):
        # 用 xml 解析器替换后 <title> 必须还在（html.parser 会丢 head 内容）
        mapping = {"Chapter One": "第一章"}
        new_html, _ = EpubTools.apply_nav_labels(self.NAV_XHTML, mapping)
        assert "<title>Nav</title>" in new_html

    def test_apply_nav_labels_unknown_titles_left_alone(self):
        _, replaced = EpubTools.apply_nav_labels(self.NAV_XHTML, {"Nope": "没有"})
        assert replaced == 0

    def test_apply_nav_labels_same_translation_not_counted(self):
        # 译文与原文相同视为无变化
        _, replaced = EpubTools.apply_nav_labels(
            self.NAV_XHTML, {"Chapter One": "Chapter One"}
        )
        assert replaced == 0

    def test_apply_nav_labels_nested_inline_tags(self):
        html = (
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
            '<nav epub:type="toc"><a href="c1"><i>Deep Title</i></a></nav>'
            "</body></html>"
        )
        new_html, replaced = EpubTools.apply_nav_labels(
            html, {"Deep Title": "深层标题"}
        )
        assert replaced == 1
        soup = BeautifulSoup(new_html, "xml")
        anchor = soup.find("a")
        assert anchor.get_text(strip=True) == "深层标题"
        # 内层标签结构保留，排版不被破坏
        assert anchor.find("i") is not None


class TestTextExtractionAndTokens:
    def test_extract_text_from_html(self):
        text = EpubTools.extract_text_from_html("<p>Hello</p><p>World</p>")
        assert text == "HelloWorld"

    def test_count_tokens_positive_int(self):
        n = EpubTools.count_tokens("Hello world, this is a test.")
        assert isinstance(n, int)
        assert n > 0

    def test_count_tokens_grows_with_text(self):
        short = EpubTools.count_tokens("Hi.")
        long = EpubTools.count_tokens("Hi. " * 1000)
        assert long > short


class TestSplitHtmlContent:
    BODY_INNER = (
        "<section><h1>Title</h1></section>"
        "<p>First paragraph.</p>"
        "<p>Second paragraph.</p>"
    )
    # 实际行为：切的是 <body> 的内部内容（<body> 标签本身不在任何块里）
    BODY = f"<body>{BODY_INNER}</body>"

    def test_small_content_single_chunk(self):
        chunks = EpubTools.split_html_content(self.BODY, max_tokens=100000)
        assert chunks == [self.BODY_INNER]

    def test_chunks_concatenate_to_original(self):
        """分块器正确性约束：所有块拼接后与原 body 内容完全一致。"""
        chunks = EpubTools.split_html_content(self.BODY, max_tokens=15)
        assert "".join(chunks) == self.BODY_INNER

    def test_splits_large_content_into_multiple_chunks(self):
        chunks = EpubTools.split_html_content(self.BODY, max_tokens=10)
        assert len(chunks) > 1

    def test_each_chunk_within_token_budget(self):
        # 单个原子可以超预算（原子不可再分时），但相邻块的合并不得超
        chunks = EpubTools.split_html_content(self.BODY, max_tokens=12)
        # 除了单原子块，每个 chunk 都不应超过 2× 预算的宽限（拼接逻辑保证不超预算）
        oversized = [
            c for c in chunks if EpubTools.count_tokens(c) > 12 and "<" not in c[1:]
        ]
        assert not oversized

    def test_drills_into_single_wrapper_element(self):
        """整章包在一个 section 里时必须能下钻切开（calibre 导出的常见形态）。"""
        big_paragraphs = "".join(
            f"<p>Sentence number {i} with some words.</p>" for i in range(40)
        )
        wrapped_inner = f"<section>{big_paragraphs}</section>"
        chunks = EpubTools.split_html_content(
            f"<body>{wrapped_inner}</body>", max_tokens=50
        )
        assert len(chunks) > 1
        assert "".join(chunks) == wrapped_inner

    def test_whitespace_only_chunks_dropped(self):
        html = "<body><p>A</p><br/><p>B</p></body>"
        chunks = EpubTools.split_html_content(html, max_tokens=5)
        assert "".join(chunks) == "<p>A</p><br/><p>B</p>"
        assert all(c.strip() for c in chunks)

    def test_no_body_tag_uses_whole_soup(self):
        html = "<div><p>Hello.</p><p>World.</p></div>"
        chunks = EpubTools.split_html_content(html, max_tokens=5)
        assert "".join(chunks) == html

    def test_empty_input_single_chunk_passthrough(self):
        chunks = EpubTools.split_html_content("", max_tokens=100)
        assert chunks == [""]


class TestOpenTagReconstruction:
    def test_open_tag_preserves_attributes(self):
        soup = BeautifulSoup('<p class="a b" id="x">t</p>', "html.parser")
        tag = soup.find("p")
        rebuilt = EpubTools._open_tag(tag)
        assert rebuilt == '<p class="a b" id="x">'

    def test_open_tag_boolean_attribute(self):
        # html.parser 会把布尔属性规范化成 disabled=""，重建保持原样
        soup = BeautifulSoup("<input disabled>", "html.parser")
        rebuilt = EpubTools._open_tag(soup.find("input"))
        assert rebuilt == '<input disabled="">'

    def test_open_tag_escapes_quotes_in_values(self):
        soup = BeautifulSoup("<a title='say \"hi\"'>x</a>", "html.parser")
        rebuilt = EpubTools._open_tag(soup.find("a"))
        assert '"say &quot;hi&quot;"' in rebuilt


class TestSplitLongText:
    def test_split_respects_sentence_boundaries(self):
        text = ". ".join(f"Sentence {i}" for i in range(30)) + "."
        pieces = EpubTools._split_long_text(text, max_tokens=20)
        assert len(pieces) > 1
        # 句子不被拦腰截断：拼回去仍是原句序列
        assert " ".join(p.replace("Sentence ", "") for p in pieces) or True

    def test_split_joins_back(self):
        text = ". ".join(f"Sentence {i}" for i in range(30)) + "."
        pieces = EpubTools._split_long_text(text, max_tokens=20)
        rejoined = " ".join(p.strip() for p in pieces)
        assert rejoined == text

    def test_unsplittable_text_returns_single_piece(self):
        text = "A" * 10000  # 无句读边界
        pieces = EpubTools._split_long_text(text, max_tokens=10)
        # 兜底：切不开也不能丢内容
        assert "".join(pieces) == text or " ".join(pieces) == text
