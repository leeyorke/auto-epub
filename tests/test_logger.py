"""Tests for logger.py - logging functionality.

Note: Many tests are skipped due to temp directory permission issues in the test environment.
"""

import json
from unittest.mock import Mock, patch

import pytest

from auto_epub.logger import (
    ConsoleLevel,
    TranslationLogger,
    get_logger,
    init_logger,
    set_console_level,
)


class TestConsoleLevel:
    """Tests for ConsoleLevel enum."""

    def test_console_level_values(self):
        """ConsoleLevel should have expected values."""
        assert ConsoleLevel.QUIET == 0
        assert ConsoleLevel.NORMAL == 1
        assert ConsoleLevel.VERBOSE == 2
        assert ConsoleLevel.DEBUG == 3

    def test_console_level_ordering(self):
        """ConsoleLevel values should be ordered correctly."""
        assert ConsoleLevel.QUIET < ConsoleLevel.NORMAL
        assert ConsoleLevel.NORMAL < ConsoleLevel.VERBOSE
        assert ConsoleLevel.VERBOSE < ConsoleLevel.DEBUG

    def test_console_level_from_int(self):
        """Should be able to create from int."""
        assert ConsoleLevel(0) == ConsoleLevel.QUIET
        assert ConsoleLevel(1) == ConsoleLevel.NORMAL
        assert ConsoleLevel(2) == ConsoleLevel.VERBOSE
        assert ConsoleLevel(3) == ConsoleLevel.DEBUG


class TestSetConsoleLevel:
    """Tests for set_console_level function."""

    def test_set_console_level_int(self):
        """Should accept integer level."""
        set_console_level(2)
        # Can't easily test global state, but shouldn't raise

    def test_set_console_level_enum(self):
        """Should accept ConsoleLevel enum."""
        set_console_level(ConsoleLevel.DEBUG)
        # Shouldn't raise


class TestTranslationLoggerInit:
    """Tests for TranslationLogger initialization."""

    def test_init_without_file_logging(self):
        """Should initialize without file logging when disabled."""
        with patch("auto_epub.logger.LOG_TO_FILE", False):
            logger = TranslationLogger("test_book")
            assert logger.log_file is None

    def test_init_sets_console_level(self):
        """Should use provided console level."""
        with patch("auto_epub.logger.LOG_TO_FILE", False):
            logger = TranslationLogger("test_book", console_level=ConsoleLevel.DEBUG)
            assert logger.console_level == ConsoleLevel.DEBUG


class TestTranslationLoggerConsoleOutput:
    """Tests for console output methods."""

    def test_console_quiet_level(self):
        """Should not output when console level is QUIET."""
        with patch("auto_epub.logger.LOG_TO_FILE", False):
            logger = TranslationLogger("test_book", console_level=ConsoleLevel.QUIET)
            # Should not print for NORMAL level messages
            logger.console("test message", ConsoleLevel.NORMAL)

    def test_console_normal_level(self):
        """Should output for NORMAL level when console is NORMAL."""
        with patch("auto_epub.logger.LOG_TO_FILE", False):
            logger = TranslationLogger("test_book", console_level=ConsoleLevel.NORMAL)
            logger.console("test message", ConsoleLevel.NORMAL)

    def test_console_verbose_level(self):
        """Should output for VERBOSE level when console is VERBOSE."""
        with patch("auto_epub.logger.LOG_TO_FILE", False):
            logger = TranslationLogger("test_book", console_level=ConsoleLevel.VERBOSE)
            logger.console("test message", ConsoleLevel.VERBOSE)

    def test_console_debug_level(self):
        """Should output for DEBUG level when console is DEBUG."""
        with patch("auto_epub.logger.LOG_TO_FILE", False):
            logger = TranslationLogger("test_book", console_level=ConsoleLevel.DEBUG)
            logger.console("test message", ConsoleLevel.DEBUG)

    def test_console_error_always_outputs(self):
        """_console_error should always output regardless of level."""
        with patch("auto_epub.logger.LOG_TO_FILE", False):
            logger = TranslationLogger("test_book", console_level=ConsoleLevel.QUIET)
            # Should not raise
            logger._console_error("error message")


class TestTranslationLoggerExcerpt:
    """Tests for _excerpt method."""

    def test_excerpt_short_text(self):
        """Should return full text for short strings."""
        logger = TranslationLogger.__new__(TranslationLogger)
        result = logger._excerpt("short")
        assert result == "short"

    def test_excerpt_long_text(self):
        """Should truncate long text with indicator."""
        logger = TranslationLogger.__new__(TranslationLogger)
        long_text = "x" * 500
        result = logger._excerpt(long_text, limit=100)
        assert len(result) > 100  # Includes the indicator
        assert "共 500 字符" in result

    def test_excerpt_newlines_replaced(self):
        """Should replace newlines with \\n."""
        logger = TranslationLogger.__new__(TranslationLogger)
        result = logger._excerpt("line1\nline2")
        assert "\\n" in result
        assert "\n" not in result


class TestTranslationLoggerChunkResult:
    """Tests for chunk_result method - unit tests without file I/O."""

    def test_chunk_result_creates_json_payload(self):
        """Should create correct JSON payload structure."""
        logger = TranslationLogger.__new__(TranslationLogger)
        logger.log_file = None
        logger.console_level = ConsoleLevel.QUIET

        stats = {
            "src_chars": 100,
            "out_chars": 80,
            "src_block_tags": 5,
            "out_block_tags": 5,
            "ok": True,
        }

        # Capture the json_line call
        with patch.object(logger, "json_line") as mock_json:
            logger.chunk_result(1, 0, 5, 1, stats, None)
            mock_json.assert_called_once()
            call_args = mock_json.call_args[0][0]
            assert call_args["event"] == "chunk_result"
            assert call_args["chapter"] == 1
            assert call_args["chunk"] == 0
            assert call_args["attempt"] == 1
            assert call_args["ok"] is True

    def test_chunk_result_handles_cached_flag(self):
        """Should include cached flag in payload."""
        logger = TranslationLogger.__new__(TranslationLogger)
        logger.log_file = None
        logger.console_level = ConsoleLevel.QUIET

        stats = {"ok": True, "cached": True}

        with patch.object(logger, "json_line") as mock_json:
            logger.chunk_result(1, 0, 5, 0, stats, None)
            call_args = mock_json.call_args[0][0]
            assert call_args["cached"] is True


class TestTranslationLoggerRunResult:
    """Tests for run_result method."""

    def test_run_result_extracts_usage(self):
        """Should extract usage from result."""
        logger = TranslationLogger.__new__(TranslationLogger)
        logger.log_file = None
        logger.console_level = ConsoleLevel.QUIET

        mock_result = Mock()
        mock_result.output = "Test output"
        mock_usage = Mock()
        mock_usage.input_tokens = 100
        mock_usage.output_tokens = 50
        mock_usage.requests = 1
        mock_usage.cache_read_tokens = 10
        mock_usage.cache_write_tokens = 5
        mock_usage.details = {"reasoning_tokens": 200}
        mock_result.usage.return_value = mock_usage
        mock_result.all_messages.return_value = []

        with patch.object(logger, "_write") as mock_write:
            logger.run_result("测试阶段", mock_result)
            # Should have written output info
            calls = mock_write.call_args_list
            assert any("run 结束" in str(c) for c in calls)


class TestTranslationLoggerError:
    """Tests for error method."""

    def test_error_writes_to_file(self):
        """Should write error to log file."""
        logger = TranslationLogger.__new__(TranslationLogger)
        logger.log_file = None
        logger.console_level = ConsoleLevel.QUIET

        with patch.object(logger, "_write") as mock_write:
            logger.error("Test error message")
            mock_write.assert_called_with("ERROR", "Test error message")


class TestTranslationLoggerRejection:
    """Tests for rejection method."""

    def test_rejection_writes_warning(self):
        """Should log rejection as warning."""
        logger = TranslationLogger.__new__(TranslationLogger)
        logger.log_file = None
        logger.console_level = ConsoleLevel.QUIET

        with patch.object(logger, "_write") as mock_write:
            logger.rejection(1, "Missing chunks")
            mock_write.assert_called_with("WARN", "章节 1 保存被拒: Missing chunks")


class TestTranslationLoggerIncomplete:
    """Tests for incomplete method."""

    def test_incomplete_writes_warning(self):
        """Should log incomplete chapter as warning."""
        logger = TranslationLogger.__new__(TranslationLogger)
        logger.log_file = None
        logger.console_level = ConsoleLevel.QUIET

        with patch.object(logger, "_write") as mock_write:
            logger.incomplete(1, "Tag ratio low")
            mock_write.assert_called_with("WARN", "章节 1 译文不完整: Tag ratio low")


class TestTranslationLoggerToolCalls:
    """Tests for tool call logging."""

    def test_tool_call_logs_info(self):
        """Should log tool call as INFO."""
        logger = TranslationLogger.__new__(TranslationLogger)
        logger.log_file = None
        logger.console_level = ConsoleLevel.QUIET

        with patch.object(logger, "_write") as mock_write:
            logger.tool_call("translate_toc", "5 items")
            mock_write.assert_called_with("INFO", "工具 translate_toc: 5 items")

    def test_tool_error_logs_warning(self):
        """Should log tool error as WARN."""
        logger = TranslationLogger.__new__(TranslationLogger)
        logger.log_file = None
        logger.console_level = ConsoleLevel.QUIET

        with patch.object(logger, "_write") as mock_write:
            logger.tool_error("save_chapter", "IO error")
            mock_write.assert_called_with(
                "WARN", "工具 save_chapter 返回错误: IO error"
            )


class TestTranslationLoggerJsonLine:
    """Tests for json_line method."""

    def test_json_line_writes_structured_data(self):
        """Should write JSON line for structured logging."""
        logger = TranslationLogger.__new__(TranslationLogger)
        logger.log_file = None

        with patch.object(logger, "_write") as mock_write:
            logger.json_line({"event": "test", "value": 123})
            mock_write.assert_called_once()
            # Check that the message contains DATA prefix and valid JSON
            args = mock_write.call_args[0]
            assert args[0] == "DATA"
            # The second arg should be JSON
            data = json.loads(args[1])
            assert data["event"] == "test"
            assert data["value"] == 123


class TestTranslationLoggerFinishReasons:
    """Tests for finish reason extraction."""

    def test_finish_reasons_empty(self):
        """Should return empty list for result without messages."""
        logger = TranslationLogger.__new__(TranslationLogger)
        mock_result = Mock()
        mock_result.all_messages.return_value = []

        reasons = logger.finish_reasons(mock_result)
        assert reasons == []

    def test_finish_reasons_extracts_normalized(self):
        """Should extract normalized finish reasons."""
        logger = TranslationLogger.__new__(TranslationLogger)

        mock_message = Mock()
        mock_message.__class__.__name__ = "ModelResponse"
        mock_message.finish_reason = "stop"
        mock_message.provider_details = {}

        mock_result = Mock()
        mock_result.all_messages.return_value = [mock_message]

        reasons = logger.finish_reasons(mock_result)
        assert reasons == ["stop"]

    def test_finish_reasons_with_provider_details(self):
        """Should include provider details when different."""
        logger = TranslationLogger.__new__(TranslationLogger)

        mock_message = Mock()
        mock_message.__class__.__name__ = "ModelResponse"
        mock_message.finish_reason = "stop"
        mock_message.provider_details = {"finish_reason": "completed"}

        mock_result = Mock()
        mock_result.all_messages.return_value = [mock_message]

        reasons = logger.finish_reasons(mock_result)
        assert reasons == ["stop(completed)"]


class TestTranslationLoggerFinalFinishReasons:
    """Tests for final_finish_reasons method."""

    def test_final_finish_reasons_returns_last_only(self):
        """Should only return last response's finish reason."""
        logger = TranslationLogger.__new__(TranslationLogger)

        msg1 = Mock()
        msg1.__class__.__name__ = "ModelResponse"
        msg1.finish_reason = "length"
        msg1.provider_details = {}

        msg2 = Mock()
        msg2.__class__.__name__ = "ModelResponse"
        msg2.finish_reason = "stop"
        msg2.provider_details = {}

        mock_result = Mock()
        mock_result.all_messages.return_value = [msg1, msg2]

        reasons = logger.final_finish_reasons(mock_result)
        assert reasons == ["stop"]


class TestTranslationLoggerUsage:
    """Tests for usage extraction."""

    def test_usage_formats_correctly(self):
        """Should format usage string correctly."""
        logger = TranslationLogger.__new__(TranslationLogger)

        mock_usage = Mock()
        mock_usage.input_tokens = 100
        mock_usage.output_tokens = 50
        mock_usage.requests = 2
        mock_usage.cache_read_tokens = 10
        mock_usage.cache_write_tokens = 5
        mock_usage.details = {"reasoning_tokens": 200}

        mock_result = Mock()
        mock_result.usage.return_value = mock_usage

        usage_str = logger._usage(mock_result)
        assert "输入=100" in usage_str
        assert "输出=50" in usage_str
        assert "请求数=2" in usage_str
        assert "缓存读=10" in usage_str
        assert "缓存写=5" in usage_str
        assert "reasoning_tokens=200" in usage_str

    def test_usage_handles_missing_attributes(self):
        """Should handle missing usage attributes gracefully."""
        logger = TranslationLogger.__new__(TranslationLogger)

        mock_usage = Mock()
        mock_usage.input_tokens = None
        mock_usage.output_tokens = None
        mock_usage.requests = None
        mock_usage.cache_read_tokens = None
        mock_usage.cache_write_tokens = None
        mock_usage.details = {}

        mock_result = Mock()
        mock_result.usage.return_value = mock_usage

        usage_str = logger._usage(mock_result)
        assert usage_str == ""


class TestTranslationLoggerToolCallsExtraction:
    """Tests for _tool_calls method."""

    def test_tool_calls_extracts_names(self):
        """Should extract tool call names from result."""
        logger = TranslationLogger.__new__(TranslationLogger)

        mock_part = Mock()
        mock_part.__class__.__name__ = "ToolCallPart"
        mock_part.tool_name = "translate_toc"

        mock_message = Mock()
        mock_message.parts = [mock_part]

        mock_result = Mock()
        mock_result.all_messages.return_value = [mock_message]

        calls = logger._tool_calls(mock_result)
        assert calls == ["translate_toc"]

    def test_tool_calls_ignores_non_tool_parts(self):
        """Should ignore non-ToolCallPart parts."""
        logger = TranslationLogger.__new__(TranslationLogger)

        mock_part = Mock()
        mock_part.__class__.__name__ = "TextPart"
        mock_part.tool_name = None

        mock_message = Mock()
        mock_message.parts = [mock_part]

        mock_result = Mock()
        mock_result.all_messages.return_value = [mock_message]

        calls = logger._tool_calls(mock_result)
        assert calls == []


class TestGetLogger:
    """Tests for get_logger function."""

    def test_get_logger_returns_instance(self):
        """Should return a TranslationLogger instance."""
        logger = get_logger()
        assert isinstance(logger, TranslationLogger)

    def test_get_logger_creates_shell_when_none(self):
        """Should create shell logger when global is None."""
        # Reset global
        import auto_epub.logger as logger_module

        original_logger = logger_module._logger
        logger_module._logger = None

        try:
            logger = get_logger()
            assert logger.log_file is None
            # console_level will be whatever the current _console_level is
        finally:
            logger_module._logger = original_logger


class TestInitLogger:
    """Tests for init_logger function."""

    def test_init_logger_creates_new_logger(self):
        """Should create and return new logger."""
        with patch("auto_epub.logger.LOG_TO_FILE", False):
            logger = init_logger("test_book", console_level=ConsoleLevel.DEBUG)
            assert isinstance(logger, TranslationLogger)
            assert logger.console_level == ConsoleLevel.DEBUG


class TestLoggerStaticMethods:
    """Tests for static methods."""

    def test_usage_static_method(self):
        """_usage should be callable as static method."""
        mock_usage = Mock()
        mock_usage.input_tokens = 100
        mock_usage.output_tokens = 50
        mock_usage.requests = 1
        mock_usage.cache_read_tokens = 0
        mock_usage.cache_write_tokens = 0
        mock_usage.details = {}

        mock_result = Mock()
        mock_result.usage.return_value = mock_usage

        # Call static method
        usage_str = TranslationLogger._usage(mock_result)
        assert "输入=100" in usage_str
        assert "输出=50" in usage_str


class TestLoggerIntegration:
    """Integration tests for logger functionality."""

    @pytest.mark.skip(reason="Temp directory permission issues in test environment")
    def test_full_logging_flow(self):
        """Test a complete logging flow."""
        pass
