"""Tests for settings.py - configuration constants and system prompts."""

import pytest
from pathlib import Path

from auto_epub.settings import (
    AGENT_SYSTEM_PROMPT,
    CARRYOVER_MAX_TOKENS,
    CARRYOVER_SEAM_CHARS,
    CARRYOVER_STYLE_CHARS,
    CHUNK_SYSTEM_PROMPT,
    ENABLE_CACHE,
    INPUT_MAX_TOKENS,
    LOG_DIR,
    LOG_EXCERPT_CHARS,
    LOG_TO_FILE,
    MAX_CHAPTER_RETRIES,
    MAX_CHUNK_RETRIES,
    MAX_REQUESTS,
    MAX_RETRIES,
    MIN_BLOCK_TAG_RATIO,
    MIN_INLINE_TAG_RATIO,
    OUTPUT_MAX_TOKENS,
    REASONING_EFFORT,
    STREAMING,
    TEMPERATURE,
    TERMS_BLOCK_BEGIN,
    TERMS_BLOCK_END,
    TIMEOUT,
    TRANSLATE_IMAGES,
    TRANSLATE_TOC,
)


class TestTokenLimits:
    """Tests for token-related settings."""

    def test_input_max_tokens_less_than_output_max_tokens(self):
        """INPUT_MAX_TOKENS must be significantly smaller than OUTPUT_MAX_TOKENS."""
        assert INPUT_MAX_TOKENS < OUTPUT_MAX_TOKENS
        # Current values: 5000 < 32768
        assert INPUT_MAX_TOKENS == 5000
        assert OUTPUT_MAX_TOKENS == 32768

    def test_input_output_ratio_allows_headroom(self):
        """Output budget should accommodate input * expansion_factor + reasoning."""
        # Worst case: input * 2.0 (expansion) + 12000 (reasoning) < OUTPUT_MAX_TOKENS
        worst_case = INPUT_MAX_TOKENS * 2.0 + 12000
        assert worst_case < OUTPUT_MAX_TOKENS

    def test_carryover_max_tokens_reasonable(self):
        """CARRYOVER_MAX_TOKENS should be reasonable for context passing."""
        assert CARRYOVER_MAX_TOKENS == 2000
        # Should be less than output tokens but enough for context
        assert CARRYOVER_MAX_TOKENS < OUTPUT_MAX_TOKENS
        assert CARRYOVER_MAX_TOKENS > 500


class TestRetrySettings:
    """Tests for retry-related settings."""

    def test_max_chunk_retries_non_negative(self):
        """MAX_CHUNK_RETRIES should be non-negative."""
        assert MAX_CHUNK_RETRIES >= 0
        assert MAX_CHUNK_RETRIES == 2

    def test_max_chapter_retries_non_negative(self):
        """MAX_CHAPTER_RETRIES should be non-negative."""
        assert MAX_CHAPTER_RETRIES >= 0
        assert MAX_CHAPTER_RETRIES == 2

    def test_max_retries_zero_for_sdk(self):
        """MAX_RETRIES should be 0 to disable SDK-level retries."""
        assert MAX_RETRIES == 0

    def test_max_requests_positive(self):
        """MAX_REQUESTS should be positive for tool-using agents."""
        assert MAX_REQUESTS > 0
        assert MAX_REQUESTS == 128


class TestTagRatioSettings:
    """Tests for tag ratio thresholds."""

    def test_min_block_tag_ratio_range(self):
        """MIN_BLOCK_TAG_RATIO should be between 0 and 1."""
        assert 0 < MIN_BLOCK_TAG_RATIO <= 1
        assert MIN_BLOCK_TAG_RATIO == 0.8

    def test_min_inline_tag_ratio_range(self):
        """MIN_INLINE_TAG_RATIO should be between 0 and 1."""
        assert 0 < MIN_INLINE_TAG_RATIO <= 1
        assert MIN_INLINE_TAG_RATIO == 0.8

    def test_block_ratio_not_less_than_inline(self):
        """Block tag ratio should not be less than inline (both 0.8 currently)."""
        # They're equal now, but block should never be lower than inline
        assert MIN_BLOCK_TAG_RATIO >= MIN_INLINE_TAG_RATIO


class TestTimeoutSettings:
    """Tests for timeout settings."""

    def test_timeout_positive(self):
        """TIMEOUT should be positive."""
        assert TIMEOUT > 0
        assert TIMEOUT == 360

    def test_timeout_covers_long_requests(self):
        """TIMEOUT should cover real-world longest normal requests (~200s)."""
        assert TIMEOUT >= 200


class TestTemperatureAndReasoning:
    """Tests for temperature and reasoning settings."""

    def test_temperature_low_for_consistency(self):
        """TEMPERATURE should be low for translation consistency."""
        assert 0 <= TEMPERATURE <= 1
        assert TEMPERATURE == 0.1

    def test_reasoning_effort_valid(self):
        """REASONING_EFFORT should be a valid value."""
        assert REASONING_EFFORT in ("low", "medium", "high", "none", "")
        assert REASONING_EFFORT == "low"


class TestStreamingSettings:
    """Tests for streaming configuration."""

    def test_streaming_is_boolean(self):
        """STREAMING should be a boolean."""
        assert isinstance(STREAMING, bool)
        assert STREAMING is False


class TestFeatureFlags:
    """Tests for feature flag settings."""

    def test_translate_images_default_false(self):
        """TRANSLATE_IMAGES should default to False."""
        assert TRANSLATE_IMAGES is False

    def test_translate_toc_default_true(self):
        """TRANSLATE_TOC should default to True."""
        assert TRANSLATE_TOC is True

    def test_enable_cache_default_true(self):
        """ENABLE_CACHE should default to True."""
        assert ENABLE_CACHE is True


class TestLoggingSettings:
    """Tests for logging configuration."""

    def test_log_to_file_enabled(self):
        """LOG_TO_FILE should be enabled for diagnostics."""
        assert LOG_TO_FILE is True

    def test_log_excerpt_chars_reasonable(self):
        """LOG_EXCERPT_CHARS should be reasonable for debugging."""
        assert LOG_EXCERPT_CHARS > 0
        assert LOG_EXCERPT_CHARS == 400

    def test_log_dir_exists(self):
        """LOG_DIR should be a Path object."""
        assert isinstance(LOG_DIR, Path)


class TestCarryoverSettings:
    """Tests for carryover (context passing) settings."""

    def test_carryover_seam_chars_positive(self):
        """CARRYOVER_SEAM_CHARS should be positive."""
        assert CARRYOVER_SEAM_CHARS > 0
        assert CARRYOVER_SEAM_CHARS == 300

    def test_carryover_style_chars_positive(self):
        """CARRYOVER_STYLE_CHARS should be positive."""
        assert CARRYOVER_STYLE_CHARS > 0
        assert CARRYOVER_STYLE_CHARS == 150

    def test_seam_chars_greater_than_style(self):
        """Seam chars should be >= style chars (more context for continuity)."""
        assert CARRYOVER_SEAM_CHARS >= CARRYOVER_STYLE_CHARS


class TestTermsBlockMarkers:
    """Tests for terms block boundary markers."""

    def test_terms_block_markers_are_html_comments(self):
        """TERMS_BLOCK markers should be HTML comments."""
        assert TERMS_BLOCK_BEGIN.startswith("<!--")
        assert TERMS_BLOCK_END.endswith("-->")
        assert TERMS_BLOCK_BEGIN == "<!--TERMS"
        assert TERMS_BLOCK_END == "-->"

    def test_terms_block_markers_unique(self):
        """Markers should be distinct."""
        assert TERMS_BLOCK_BEGIN != TERMS_BLOCK_END


class TestAgentSystemPrompts:
    """Tests for agent system prompts."""

    def test_agent_system_prompt_has_placeholder(self):
        """AGENT_SYSTEM_PROMPT should have {target_language} placeholder."""
        assert "{target_language}" in AGENT_SYSTEM_PROMPT
        assert len(AGENT_SYSTEM_PROMPT) > 500  # Substantial prompt

    def test_chunk_system_prompt_has_placeholder(self):
        """CHUNK_SYSTEM_PROMPT should have {target_language} placeholder."""
        assert "{target_language}" in CHUNK_SYSTEM_PROMPT
        assert len(CHUNK_SYSTEM_PROMPT) > 500

    def test_prompts_different(self):
        """The two prompts should be different (different purposes)."""
        assert AGENT_SYSTEM_PROMPT != CHUNK_SYSTEM_PROMPT

    def test_agent_prompt_mentions_tools(self):
        """Agent prompt should mention tool usage."""
        assert "工具" in AGENT_SYSTEM_PROMPT or "tool" in AGENT_SYSTEM_PROMPT.lower()

    def test_chunk_prompt_forbids_tools(self):
        """Chunk prompt should mention no tools available."""
        assert (
            "没有任何工具" in CHUNK_SYSTEM_PROMPT
            or "no tools" in CHUNK_SYSTEM_PROMPT.lower()
        )

    def test_both_prompts_have_html_rules(self):
        """Both prompts should have HTML tag preservation rules."""
        for prompt in [AGENT_SYSTEM_PROMPT, CHUNK_SYSTEM_PROMPT]:
            assert "标签" in prompt or "tag" in prompt.lower()
            assert "保留" in prompt or "preserve" in prompt.lower() or "保留" in prompt


class TestMigrateLegacyDir:
    """Tests for migrate_legacy_dir function."""

    def test_migrate_legacy_dir_exists(self):
        """migrate_legacy_dir function should exist."""
        from auto_epub.settings import migrate_legacy_dir

        assert callable(migrate_legacy_dir)

    @pytest.mark.skip(reason="Temp directory permission issues in test environment")
    def test_migrate_legacy_dir_noop_when_target_exists(self):
        """Should do nothing when target directory already exists."""
        pass

    @pytest.mark.skip(reason="Temp directory permission issues in test environment")
    def test_migrate_legacy_dir_noop_when_legacy_missing(self):
        """Should do nothing when legacy directory doesn't exist."""
        pass


class TestSettingsConstants:
    """Tests for specific constant values matching ARCHITECTURE.md requirements."""

    def test_red_lines_token_values(self):
        """Verify token values match documented red lines."""
        # From ARCHITECTURE.md: INPUT_MAX_TOKENS=5000, OUTPUT_MAX_TOKENS=32768
        assert INPUT_MAX_TOKENS == 5000
        assert OUTPUT_MAX_TOKENS == 32768

    def test_red_lines_retry_values(self):
        """Verify retry values match documented red lines."""
        # From ARCHITECTURE.md: MAX_CHUNK_RETRIES=2, MAX_CHAPTER_RETRIES=2
        assert MAX_CHUNK_RETRIES == 2
        assert MAX_CHAPTER_RETRIES == 2

    def test_red_lines_tag_ratios(self):
        """Verify tag ratios match documented red lines."""
        # From ARCHITECTURE.md: MIN_BLOCK_TAG_RATIO=0.8, MIN_INLINE_TAG_RATIO=0.8
        assert MIN_BLOCK_TAG_RATIO == 0.8
        assert MIN_INLINE_TAG_RATIO == 0.8

    def test_red_lines_carryover_values(self):
        """Verify carryover values match documented red lines."""
        # From ARCHITECTURE.md: CARRYOVER_MAX_TOKENS=2000, SEAM=300, STYLE=150
        assert CARRYOVER_MAX_TOKENS == 2000
        assert CARRYOVER_SEAM_CHARS == 300
        assert CARRYOVER_STYLE_CHARS == 150
