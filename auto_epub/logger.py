"""
诊断日志

翻译失败（尤其是模型输出被截断、工具调用泄漏成文本）在控制台上
往往只留下一行错误，无法定位原因。这里把每章的分块尺寸、模型输出、
token 用量（含 reasoning_tokens）、结束原因、工具调用序列完整记录到
文件，失败后可回查。

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
        # 记下当前尝试次数，落盘的诊断文件用它区分同一章的多次尝试
        self._attempt = attempt
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
        """记录一次 Agent run 的输出、token 用量、结束原因和工具调用序列"""
        output = getattr(result, "output", "") or ""
        self._write("INFO", f"章节 {index} run 结束，输出 {len(output)} 字符")
        self._write("DEBUG", f"  输出内容: {self._excerpt(output)}")

        usage = self._usage(result)
        if usage:
            self._write("INFO", f"  token 用量: {usage}")
            self.console(f"    {usage}", ConsoleLevel.VERBOSE)

        # finish_reason=length 是"输出被 max_tokens 截断"的直接证据。
        # 截断会让工具调用的参数 JSON 不完整，进而退化成文本形式的工具调用，
        # 光看 output 长度是判断不出来的（推理 token 不出现在 output 里）。
        reasons = self._finish_reasons(result)
        if reasons:
            self._write("INFO", f"  结束原因: {' → '.join(reasons)}")
            truncated = sum(1 for r in reasons if "length" in r)
            if truncated:
                self._write(
                    "WARN",
                    f"  章节 {index} 有 {truncated}/{len(reasons)} 次响应被 "
                    f"max_tokens 截断（finish_reason=length）",
                )
                self.console(
                    f"  ⚠️  章节 {index} 有 {truncated} 次模型响应被 max_tokens 截断，"
                    f"需要调大 OUTPUT_MAX_TOKENS 或调小 INPUT_MAX_TOKENS"
                )

        calls = self._tool_calls(result)
        if calls:
            self._write("DEBUG", f"  工具调用({len(calls)}): {' → '.join(calls)}")
            self.console(
                f"    工具调用({len(calls)}): {' → '.join(calls)}",
                ConsoleLevel.DEBUG,
            )

    def leaked_tool_call(self, index: int, output: str) -> None:
        """模型把工具调用写成了纯文本——完整落盘以便判断是否为截断所致"""
        self._write(
            "ERROR",
            f"章节 {index} 输出了文本形式的工具调用，共 {len(output)} 字符: "
            f"{self._excerpt(output)}",
        )
        # 片段看不出结尾是否被截断，也丢掉了里面已经译好的正文，因此完整存一份
        dumped = self._dump(index, "leaked", output, "txt")
        hint = f"，原始输出已存: {dumped}" if dumped else "（详见日志文件）"
        self._console_error(f"章节 {index} 输出了文本形式的工具调用{hint}")

    def error(self, message: str) -> None:
        self._write("ERROR", message)
        self._console_error(message)

    def rejection(self, index: int, reason: str) -> None:
        self._write("WARN", f"章节 {index} 保存被拒: {reason}")
        self.console(f"  ⤺ 章节 {index} 保存被拒：{reason}", ConsoleLevel.VERBOSE)

    def incomplete(self, index: int, reason: str) -> None:
        """译文已接收但判定不完整——与"拒绝写入"区分开，便于回查漏译分布"""
        self._write("WARN", f"章节 {index} 译文不完整: {reason}")
        self.console(f"  ⚠️  章节 {index} 译文不完整：{reason}", ConsoleLevel.VERBOSE)

    def tool_call(self, tool: str, detail: str = "") -> None:
        """记录一次本身没有专门日志的工具调用

        模型会卡在"反复调用同一个工具、什么也没推进"的空转里。这类调用不
        留痕的话，整段空转在日志里就是一片空白：实测一章 208 token 的
        titlepage 空转掉 128 个请求、4 分 01 秒，而日志里只有一行"发放块 0"，
        事后无法判断它到底在调什么。
        """
        self._write("INFO", f"工具 {tool}" + (f": {detail}" if detail else ""))

    def tool_error(self, tool: str, reason: str) -> None:
        """记录工具返回给模型的错误

        工具的错误是 return 出去的字符串（不是 raise ModelRetry），pydantic-ai
        视为调用成功，既不计入 max_tool_retries 也不中断 run——唯一的刹车是
        UsageLimits 的 request_limit。也就是说这类错误天然可以无限循环，
        日志是唯一能看出循环的地方，一条都不能省。
        """
        self._write("WARN", f"工具 {tool} 返回错误: {reason}")
        self.console(f"  ⤺ {tool}: {reason}", ConsoleLevel.VERBOSE)

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
        for attr, label in (
            ("cache_read_tokens", "缓存读"),
            ("cache_write_tokens", "缓存写"),
        ):
            value = getattr(usage, attr, None)
            if value:
                parts.append(f"{label}={value}")
        # details 里藏着 reasoning_tokens：推理 token 计入 max_tokens 却不出现在
        # result.output 里，是"输出看着不长却被截断"的隐形消耗者，必须记下来
        details = getattr(usage, "details", None) or {}
        for key, value in details.items():
            if value:
                parts.append(f"{key}={value}")
        return "、".join(parts)

    @staticmethod
    def _finish_reasons(result: Any) -> list[str]:
        """提取本次 run 里每次模型响应的结束原因（括号内为供应商原值）"""
        reasons: list[str] = []
        try:
            messages = result.all_messages()
        except Exception:
            return reasons
        for message in messages:
            if type(message).__name__ != "ModelResponse":
                continue
            normalized = getattr(message, "finish_reason", None)
            raw = (getattr(message, "provider_details", None) or {}).get(
                "finish_reason"
            )
            if normalized and raw and raw != normalized:
                reasons.append(f"{normalized}({raw})")
            elif normalized or raw:
                reasons.append(str(normalized or raw))
        return reasons

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

    def _dump(self, index: int, kind: str, text: str, suffix: str) -> Optional[Path]:
        """把诊断内容原样落盘到日志同目录，返回文件路径（失败返回 None）"""
        if not self.log_file:
            return None
        attempt = getattr(self, "_attempt", 0)
        dump = self.log_file.with_name(
            f"{self.log_file.stem}_ch{index}_try{attempt}_{kind}.{suffix}"
        )
        try:
            dump.write_text(text, encoding="utf-8")
        except OSError:
            return None
        self._write("DEBUG", f"  已保存 {kind}: {dump}")
        return dump

    def dump_buffer(self, index: int, translated_html: str) -> None:
        """把被拒绝的译文原样落盘，便于人工比对漏了哪一段"""
        dump = self._dump(index, "rejected", translated_html, "html")
        if dump:
            self.console(f"  被拒译文已保存: {dump}", ConsoleLevel.DEBUG)

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
