"""
诊断日志

翻译失败（尤其是模型输出被截断、工具调用泄漏成文本）在控制台上
往往只留下一行错误，无法定位原因。这里把每章的分块尺寸、模型输出、
token 用量、工具调用序列完整记录到文件，失败后可回查。

文件日志始终记录完整信息。控制台输出由 ConsoleLevel 控制：
默认打印进度摘要和诊断概要，-v 追加细节，-q 只保留错误。
"""

import json
import sys
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Any, Optional, Union

from .settings import LOG_DIR, LOG_EXCERPT_CHARS, LOG_TO_FILE


class ConsoleLevel(IntEnum):
    """控制台输出详细程度（文件日志始终完整，不受影响）"""

    QUIET = 0  # 只输出错误
    NORMAL = 1  # 进度摘要（章节进度、缓存恢复等）
    VERBOSE = 2  # + 分块尺寸、token 用量（等同旧 DEBUG_MODE=True）
    DEBUG = 3  # + 工具调用序列、保存被拒原因、输出片段等诊断细节


# 模块级默认控制台等级；CLI 解析参数后用 set_console_level() 覆盖
_console_level = ConsoleLevel.VERBOSE


def set_console_level(level: Union[int, ConsoleLevel]) -> None:
    """设置默认控制台详细程度，影响之后创建（含空壳）的所有日志器"""
    global _console_level
    _console_level = ConsoleLevel(level)


class TranslationLogger:
    """把翻译过程的诊断信息同时输出到控制台和日志文件"""

    def __init__(
        self,
        book_name: str = "epub",
        console_level: Optional[Union[int, ConsoleLevel]] = None,
    ):
        self.console_level = (
            ConsoleLevel(console_level) if console_level is not None else _console_level
        )
        self.log_file: Optional[Path] = None
        if not LOG_TO_FILE:
            return

        log_dir = Path(LOG_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in book_name)
        self.log_file = log_dir / f"{safe_name[:60]}_{stamp}.log"
        self._write("INFO", f"日志开始: {book_name}")

    # ---------- 底层写入 ----------

    def _write(self, level: str, message: str) -> None:
        if not self.log_file:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        try:
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {level:<5} {message}\n")
        except OSError:
            # 日志失败不该影响翻译本身
            self.log_file = None

    def console(
        self, message: str = "", level: ConsoleLevel = ConsoleLevel.NORMAL
    ) -> None:
        """按控制台详细程度决定是否打印；低于当前等级的消息被丢弃"""
        if self.console_level >= level:
            stamp = datetime.now().strftime("%H:%M:%S")
            level_tag = level.name if level > ConsoleLevel.NORMAL else ""
            if level_tag:
                print(f"[{stamp}] [{level_tag:<5}] {message}")
            else:
                print(f"[{stamp}] {message}")

    def _console_error(self, message: str) -> None:
        """错误不受控制台等级限制，始终输出到 stderr"""
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] ERROR ❌ {message}", file=sys.stderr)

    @staticmethod
    def _excerpt(text: str, limit: int = LOG_EXCERPT_CHARS) -> str:
        text = (text or "").replace("\n", "\\n")
        if len(text) <= limit:
            return text
        return f"{text[:limit]}...(共 {len(text)} 字符)"

    # ---------- 对外接口 ----------

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def chapter_start(self, index: int, title: str, attempt: int) -> None:
        self._write("INFO", f"===== 章节 {index} [{title}] 第 {attempt} 次尝试 =====")

    def chunks(self, index: int, chunk_stats: list[dict[str, int]]) -> None:
        """记录每个分块的 token / 字符 / 标签数"""
        total_tokens = sum(c["tokens"] for c in chunk_stats)
        self._write(
            "INFO",
            f"章节 {index} 切分为 {len(chunk_stats)} 块，合计 {total_tokens} tokens",
        )
        for i, c in enumerate(chunk_stats, 1):
            self._write(
                "DEBUG",
                f"  块 {i}: tokens={c['tokens']} chars={c['chars']} tags={c['tags']}",
            )
        biggest = max((c["tokens"] for c in chunk_stats), default=0)
        self.console(
            f"    最大分块 {biggest} tokens，合计 {total_tokens} tokens",
            ConsoleLevel.VERBOSE,
        )

    def run_result(self, index: int, result: Any) -> None:
        """记录一次 Agent run 的输出、token 用量和工具调用序列"""
        output = getattr(result, "output", "") or ""
        self._write("INFO", f"章节 {index} run 结束，输出 {len(output)} 字符")
        self._write("DEBUG", f"  输出内容: {self._excerpt(output)}")

        usage = self._usage(result)
        if usage:
            self._write("INFO", f"  token 用量: {usage}")
            self.console(f"    {usage}", ConsoleLevel.VERBOSE)

        calls = self._tool_calls(result)
        if calls:
            self._write("DEBUG", f"  工具调用({len(calls)}): {' → '.join(calls)}")
            self.console(
                f"    工具调用({len(calls)}): {' → '.join(calls)}",
                ConsoleLevel.DEBUG,
            )

    def leaked_tool_call(self, index: int, output: str) -> None:
        """模型把工具调用写成了纯文本——记录原文以便判断是否为截断所致"""
        self._write(
            "ERROR",
            f"章节 {index} 输出了文本形式的工具调用，原始输出: "
            f"{self._excerpt(output, LOG_EXCERPT_CHARS * 3)}",
        )
        self._console_error(
            f"章节 {index} 输出了文本形式的工具调用（可能为截断，详见日志文件）"
        )

    def error(self, message: str) -> None:
        self._write("ERROR", message)
        self._console_error(message)

    def rejection(self, index: int, reason: str) -> None:
        self._write("WARN", f"章节 {index} 保存被拒: {reason}")
        self.console(f"  ⤺ 章节 {index} 保存被拒：{reason}", ConsoleLevel.VERBOSE)

    # ---------- 从 run 结果里挖信息 ----------

    @staticmethod
    def _usage(result: Any) -> str:
        try:
            usage = result.usage()
        except Exception:
            return ""
        parts = []
        for attr, label in (
            ("input_tokens", "输入"),
            ("output_tokens", "输出"),
            ("requests", "请求数"),
        ):
            value = getattr(usage, attr, None)
            if value is not None:
                parts.append(f"{label}={value}")
        return "、".join(parts)

    @staticmethod
    def _tool_calls(result: Any) -> list[str]:
        """提取本次 run 里模型实际发起的工具调用名"""
        names = []
        try:
            messages = result.all_messages()
        except Exception:
            return names
        for message in messages:
            for part in getattr(message, "parts", []):
                name = getattr(part, "tool_name", None)
                if name and type(part).__name__ == "ToolCallPart":
                    names.append(name)
        return names

    def dump_buffer(self, index: int, translated_html: str) -> None:
        """把被拒绝的译文原样落盘，便于人工比对漏了哪一段"""
        if not self.log_file:
            return
        dump = self.log_file.with_name(f"{self.log_file.stem}_ch{index}_rejected.html")
        try:
            dump.write_text(translated_html, encoding="utf-8")
            self._write("DEBUG", f"  被拒译文已保存: {dump}")
            self.console(f"  被拒译文已保存: {dump}", ConsoleLevel.DEBUG)
        except OSError:
            pass

    def json_line(self, payload: dict) -> None:
        """结构化记录，便于脚本统计失败分布"""
        self._write("DATA", json.dumps(payload, ensure_ascii=False))


# 全局单例：工具函数（agent_tools）里拿不到 translator 实例，用模块级变量共享
_logger: Optional[TranslationLogger] = None


def get_logger() -> TranslationLogger:
    """获取当前日志器，未初始化时返回一个不写文件的空壳"""
    global _logger
    if _logger is None:
        _logger = TranslationLogger.__new__(TranslationLogger)
        _logger.log_file = None
        _logger.console_level = _console_level
    return _logger


def init_logger(
    book_name: str, console_level: Optional[Union[int, ConsoleLevel]] = None
) -> TranslationLogger:
    """在一次翻译开始时初始化日志器；console_level 为 None 时用模块级默认"""
    global _logger
    _logger = TranslationLogger(book_name, console_level=console_level)
    return _logger
