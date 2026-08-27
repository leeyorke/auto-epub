"""Pytest configuration and shared fixtures for auto_epub tests."""

import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup
from ebooklib import epub


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_epub(temp_dir: Path) -> Path:
    """Create a minimal valid EPUB file for testing."""
    book = epub.EpubBook()
    book.set_identifier("test-book-123")
    book.set_title("Test Book")
    book.set_language("en")
    book.add_author("Test Author")

    # Create a simple chapter
    chapter = epub.EpubHtml(title="Chapter 1", file_name="ch01.xhtml", lang="en")
    chapter.content = b"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 1</title></head>
<body>
<h1>Chapter 1</h1>
<p>This is the first paragraph.</p>
<p>This is the second paragraph with <em>emphasis</em>.</p>
</body>
</html>"""
    book.add_item(chapter)
    book.toc = [epub.Link("ch01.xhtml", "Chapter 1", "ch01")]
    book.spine = ["nav", chapter]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    epub_path = temp_dir / "test_book.epub"
    epub.write_epub(str(epub_path), book)
    return epub_path


@pytest.fixture
def sample_html() -> str:
    """Sample HTML content for testing."""
    return """<html><body>
<h1>Test Title</h1>
<p>First paragraph with <strong>bold</strong> text.</p>
<p>Second paragraph with <a href="#note">link</a> and <em>italic</em>.</p>
<div><p>Nested paragraph.</p></div>
<ul><li>Item 1</li><li>Item 2</li></ul>
</body></html>"""


@pytest.fixture
def sample_chunks() -> list[str]:
    """Sample HTML chunks for testing."""
    return [
        "<h1>Test Title</h1>",
        "<p>First paragraph with <strong>bold</strong> text.</p>",
        '<p>Second paragraph with <a href="#note">link</a> and <em>italic</em>.</p>',
        "<div><p>Nested paragraph.</p></div>",
        "<ul><li>Item 1</li><li>Item 2</li></ul>",
    ]


@pytest.fixture
def mock_cache_manager():
    """Create a mock cache manager for testing."""
    mock = Mock()
    mock.get_cache_key.return_value = "test_cache_key"
    mock.load_progress.return_value = None
    mock.save_progress.return_value = None
    mock.update_progress.return_value = None
    mock.load_chapter.return_value = None
    mock.save_chapter.return_value = None
    mock.load_chunk.return_value = None
    mock.save_chunk.return_value = None
    mock.load_image.return_value = None
    mock.save_image.return_value = None
    mock.clear_cache.return_value = None
    mock.clear_all.return_value = 0
    return mock


@pytest.fixture
def mock_epub_context(mock_cache_manager):
    """Create a mock EpubContext for testing."""
    from auto_epub.agent_tools import EpubContext
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_title("Test Book")
    book.set_language("en")

    chapter = epub.EpubHtml(title="Chapter 1", file_name="ch01.xhtml", lang="en")
    chapter.content = (
        b"<html><body><h1>Chapter 1</h1><p>Test content.</p></body></html>"
    )
    book.add_item(chapter)

    ctx = EpubContext(
        book=book,
        target_language="zh",
        cache_key="test_key",
        cache_manager=mock_cache_manager,
        glossary={},
    )
    return ctx


# ============================================================================
# Test Utilities
# ============================================================================


def create_test_epub(path: Path, chapters: list[dict] = None) -> epub.EpubBook:
    """Helper to create a test EPUB with given chapters."""
    book = epub.EpubBook()
    book.set_identifier("test-book")
    book.set_title("Test Book")
    book.set_language("en")
    book.add_author("Test Author")

    if chapters is None:
        chapters = [
            {"title": "Chapter 1", "content": "<h1>Chapter 1</h1><p>Content 1</p>"},
            {"title": "Chapter 2", "content": "<h1>Chapter 2</h1><p>Content 2</p>"},
        ]

    epub_chapters = []
    for i, ch in enumerate(chapters, 1):
        chapter = epub.EpubHtml(
            title=ch["title"], file_name=f"ch{i:02d}.xhtml", lang="en"
        )
        chapter.content = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{ch["title"]}</title></head>
<body>{ch["content"]}</body>
</html>""".encode("utf-8")
        book.add_item(chapter)
        epub_chapters.append(chapter)

    book.toc = [epub.Link(ch.file_name, ch.title, ch.get_id()) for ch in epub_chapters]
    book.spine = ["nav"] + epub_chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    epub.write_epub(str(path), book)
    return book


def parse_html(html: str) -> BeautifulSoup:
    """Parse HTML string with BeautifulSoup."""
    return BeautifulSoup(html, "html.parser")


def count_tags(html: str, block_only: bool = False) -> int:
    """Count tags in HTML using the same regex as the project."""
    import re

    TAG_NAME_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*>")
    BLOCK_TAGS = frozenset(
        {
            "p",
            "div",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "li",
            "ul",
            "ol",
            "dl",
            "dt",
            "dd",
            "blockquote",
            "table",
            "thead",
            "tbody",
            "tfoot",
            "tr",
            "td",
            "th",
            "section",
            "article",
            "aside",
            "header",
            "footer",
            "figure",
            "figcaption",
            "pre",
            "hr",
            "img",
        }
    )
    tags = TAG_NAME_RE.findall(html)
    if not block_only:
        return len(tags)
    return sum(1 for _, name in tags if name.lower() in BLOCK_TAGS)


# ============================================================================
# Pytest Configuration
# ============================================================================


def pytest_configure(config):
    """Configure pytest."""
    config.addinivalue_line(
        "markers",
        "integration: mark test as integration test requiring external services",
    )
    config.addinivalue_line("markers", "slow: mark test as slow running")
