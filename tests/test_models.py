"""Tests for data models (models.py)."""

from auto_epub.models import (
    ChapterTranslation,
    ImageTranslationResult,
    TranslationProgress,
    TranslationResult,
)


class TestTranslationResult:
    """Tests for TranslationResult model."""

    def test_translation_result_minimal(self):
        """Test creating TranslationResult with only required fields."""
        result = TranslationResult(translated_text="Hello world")
        assert result.translated_text == "Hello world"
        assert result.new_terms == {}

    def test_translation_result_with_terms(self):
        """Test creating TranslationResult with new_terms."""
        terms = {"Hello": "你好", "world": "世界"}
        result = TranslationResult(translated_text="你好世界", new_terms=terms)
        assert result.translated_text == "你好世界"
        assert result.new_terms == terms

    def test_translation_result_empty_translated_text(self):
        """Test that empty translated_text is allowed."""
        result = TranslationResult(translated_text="")
        assert result.translated_text == ""

    def test_translation_result_serialization(self):
        """Test model serialization to dict."""
        result = TranslationResult(translated_text="Test", new_terms={"key": "value"})
        data = result.model_dump()
        assert data["translated_text"] == "Test"
        assert data["new_terms"] == {"key": "value"}

    def test_translation_result_deserialization(self):
        """Test model deserialization from dict."""
        data = {"translated_text": "Test", "new_terms": {"a": "b"}}
        result = TranslationResult(**data)
        assert result.translated_text == "Test"
        assert result.new_terms == {"a": "b"}


class TestChapterTranslation:
    """Tests for ChapterTranslation model."""

    def test_chapter_translation_minimal(self):
        """Test creating ChapterTranslation with required fields."""
        chapter = ChapterTranslation(
            chapter_id="ch01",
            title="Chapter 1",
            original_content="<p>Original</p>",
            translated_content="<p>Translated</p>",
        )
        assert chapter.chapter_id == "ch01"
        assert chapter.title == "Chapter 1"
        assert chapter.original_content == "<p>Original</p>"
        assert chapter.translated_content == "<p>Translated</p>"
        assert chapter.status == "pending"

    def test_chapter_translation_with_status(self):
        """Test creating ChapterTranslation with custom status."""
        chapter = ChapterTranslation(
            chapter_id="ch01",
            title="Chapter 1",
            original_content="<p>Original</p>",
            translated_content="<p>Translated</p>",
            status="completed",
        )
        assert chapter.status == "completed"

    def test_chapter_translation_invalid_status(self):
        """Test that invalid status is accepted (no enum validation)."""
        # Status is a plain string, not an enum
        chapter = ChapterTranslation(
            chapter_id="ch01",
            title="Chapter 1",
            original_content="<p>Original</p>",
            translated_content="<p>Translated</p>",
            status="invalid_status",
        )
        assert chapter.status == "invalid_status"


class TestImageTranslationResult:
    """Tests for ImageTranslationResult model."""

    def test_image_translation_result_minimal(self):
        """Test creating ImageTranslationResult with minimal fields."""
        result = ImageTranslationResult(has_text=False)
        assert result.has_text is False
        assert result.original_texts == []
        assert result.translated_texts == []
        assert result.new_image_base64 is None

    def test_image_translation_result_with_text(self):
        """Test creating ImageTranslationResult with text content."""
        result = ImageTranslationResult(
            has_text=True,
            original_texts=["Hello", "World"],
            translated_texts=["你好", "世界"],
            new_image_base64="base64data",
        )
        assert result.has_text is True
        assert result.original_texts == ["Hello", "World"]
        assert result.translated_texts == ["你好", "世界"]
        assert result.new_image_base64 == "base64data"

    def test_image_translation_result_empty_lists_default(self):
        """Test that empty lists are default for text fields."""
        result = ImageTranslationResult(has_text=True)
        assert result.original_texts == []
        assert result.translated_texts == []


class TestTranslationProgress:
    """Tests for TranslationProgress model."""

    def test_translation_progress_minimal(self):
        """Test creating TranslationProgress with required fields."""
        progress = TranslationProgress(
            source_lang="en", target_lang="zh", total_chapters=10
        )
        assert progress.source_lang == "en"
        assert progress.target_lang == "zh"
        assert progress.total_chapters == 10
        assert progress.completed_chapters == []
        assert progress.failed_chapters == []
        assert progress.glossary == {}
        assert progress.toc_translated is False
        assert progress.toc_titles == []
        assert progress.images_translated == {}
        assert progress.book_name == ""

    def test_translation_progress_with_all_fields(self):
        """Test creating TranslationProgress with all fields."""
        progress = TranslationProgress(
            book_name="Test Book",
            source_lang="en",
            target_lang="zh",
            total_chapters=10,
            completed_chapters=["ch01", "ch02"],
            failed_chapters=["ch03"],
            glossary={"Hello": "你好"},
            toc_translated=True,
            toc_titles=["Chapter 1", "Chapter 2"],
            images_translated={"image1.png": True},
        )
        assert progress.book_name == "Test Book"
        assert progress.completed_chapters == ["ch01", "ch02"]
        assert progress.failed_chapters == ["ch03"]
        assert progress.glossary == {"Hello": "你好"}
        assert progress.toc_translated is True
        assert progress.toc_titles == ["Chapter 1", "Chapter 2"]
        assert progress.images_translated == {"image1.png": True}

    def test_translation_progress_serialization(self):
        """Test model serialization to JSON."""
        progress = TranslationProgress(
            source_lang="en",
            target_lang="zh",
            total_chapters=5,
            completed_chapters=["ch01"],
        )
        json_str = progress.model_dump_json()
        assert "en" in json_str
        assert "zh" in json_str
        assert "ch01" in json_str

    def test_translation_progress_deserialization(self):
        """Test model deserialization from JSON."""
        json_str = '{"source_lang": "en", "target_lang": "zh", "total_chapters": 5, "completed_chapters": ["ch01"]}'
        progress = TranslationProgress.model_validate_json(json_str)
        assert progress.source_lang == "en"
        assert progress.target_lang == "zh"
        assert progress.total_chapters == 5
        assert progress.completed_chapters == ["ch01"]


class TestModelIntegration:
    """Integration tests for model interactions."""

    def test_translation_progress_with_chapter_translations(self):
        """Test that TranslationProgress can track chapter translations."""
        progress = TranslationProgress(
            source_lang="en", target_lang="zh", total_chapters=3
        )

        # Simulate completing chapters
        progress.completed_chapters.append("ch01")
        progress.completed_chapters.append("ch02")

        assert len(progress.completed_chapters) == 2
        assert "ch01" in progress.completed_chapters

    def test_glossary_update(self):
        """Test updating glossary in progress."""
        progress = TranslationProgress(
            source_lang="en", target_lang="zh", total_chapters=5
        )
        progress.glossary["Hello"] = "你好"
        progress.glossary["World"] = "世界"

        assert progress.glossary["Hello"] == "你好"
        assert len(progress.glossary) == 2

    def test_images_translated_tracking(self):
        """Test tracking image translation status."""
        progress = TranslationProgress(
            source_lang="en", target_lang="zh", total_chapters=5
        )
        progress.images_translated["cover.jpg"] = True
        progress.images_translated["image1.png"] = False

        assert progress.images_translated["cover.jpg"] is True
        assert progress.images_translated["image1.png"] is False
