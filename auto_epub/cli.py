"""
命令行接口
"""

import asyncio
import sys
from pathlib import Path

from typer import Argument, Exit, Option, Typer
from typing_extensions import Annotated

from . import __version__
from .client import create_translator
from .logger import ConsoleLevel, get_logger, set_console_level
from .settings import ENABLE_CACHE, TRANSLATE_IMAGES, TRANSLATE_TOC


def _force_utf8_output() -> None:
    """把标准输出改成 UTF-8，并对无法编码的字符降级而不是抛错。

    Windows 控制台默认是 GBK，输出里的 emoji 会让 print 抛
    UnicodeEncodeError 直接中断翻译——重定向到文件时尤其容易触发。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


_force_utf8_output()

app = Typer(
    name="epub-translator", help="🌍 EPUB 电子书翻译工具 - 支持并发、缓存、图片翻译"
)


@app.command()
def translate(
    file_path: Annotated[str, Argument(help="EPUB 文件路径，例如：'book.epub'")],
    target_language: Annotated[
        str,
        Option(
            "-l",
            "--lang",
            help="目标语言代码，例如：'zh'(中文), 'en'(英文), 'ja'(日文)",
        ),
    ],
    translate_images: Annotated[
        bool, Option("--images/--no-images", help="是否翻译图片")
    ] = TRANSLATE_IMAGES,
    translate_toc: Annotated[
        bool, Option("--toc/--no-toc", help="是否翻译目录")
    ] = TRANSLATE_TOC,
    resume: Annotated[
        bool, Option("--resume/--no-resume", help="是否启用断点续传（从缓存恢复）")
    ] = True,
    verbose: Annotated[
        int,
        Option(
            "-v",
            "--verbose",
            count=True,
            help="提高控制台详细程度，可叠加：-v 显示工具调用、被拒原因等诊断细节",
        ),
    ] = 0,
    quiet: Annotated[
        bool, Option("-q", "--quiet", help="只输出错误，静默其他所有输出")
    ] = False,
) -> None:
    """
    翻译 EPUB 文件

    示例：

      # 基础翻译
      epub-translator book.epub -l zh

      # 翻译图片（需要 GPT-4V）
      epub-translator book.epub -l zh --images

      # 不翻译目录
      epub-translator book.epub -l zh --no-toc

      # 不使用缓存（重新翻译）
      epub-translator book.epub -l zh --no-resume

      # 显示诊断细节 / 只输出错误
      epub-translator book.epub -l zh -v
      epub-translator book.epub -l zh -q
    """
    if quiet and verbose:
        print("❌ 错误：-q/--quiet 与 -v/--verbose 不能同时使用")
        raise Exit(code=1)

    # 控制台详细程度：默认 VERBOSE（进度+摘要），-v 升到 DEBUG，-q 降到 QUIET。
    # 文件日志不受影响，始终完整记录。
    level = (
        ConsoleLevel.QUIET
        if quiet
        else ConsoleLevel(min(ConsoleLevel.VERBOSE + verbose, ConsoleLevel.DEBUG))
    )
    set_console_level(level)
    if not Path(file_path).exists():
        get_logger().error(f"文件不存在 - {file_path}")
        raise Exit(code=1)

    if not file_path.endswith(".epub"):
        get_logger().error(f"不是 EPUB 文件 - {file_path}")
        raise Exit(code=1)

    # 创建翻译器
    get_logger().console("\n🚀 初始化翻译器...")
    translator = create_translator(
        target_language=target_language, cache_enabled=ENABLE_CACHE and resume
    )

    # 执行翻译
    try:
        output_file = asyncio.run(
            translator.translate_epub(
                input_file=file_path,
                target_language=target_language,
                translate_images=translate_images,
                translate_toc=translate_toc,
                resume=resume,
            )
        )

        get_logger().console("\n🎉 翻译成功完成！")
        get_logger().console(f"📁 输出文件：{output_file}")

    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断翻译", file=sys.stderr)
        print("💡 提示：下次运行时使用 --resume 可以继续翻译", file=sys.stderr)

    except Exception as e:
        get_logger().error(f"翻译失败：{e}")
        import traceback

        if get_logger().console_level >= ConsoleLevel.DEBUG:
            traceback.print_exc()


@app.command()
def clear_cache(
    file_path: Annotated[str, Argument(help="EPUB 文件路径")],
    target_language: Annotated[
        str,
        Option("-l", "--lang", help="目标语言代码"),
    ],
) -> None:
    """清除指定文件的翻译缓存"""
    from .cache_manager import CacheManager

    cache_manager = CacheManager()
    cache_key = cache_manager.get_cache_key(file_path, target_language)
    cache_manager.clear_cache(cache_key)

    print(f"✓ 已清除缓存：{file_path} -> {target_language}")


@app.command()
def version() -> None:
    """显示版本信息"""
    print(f"📚 EPUB Translator {__version__}")
    print("🔗 GitHub: https://github.com/leeyorke/auto-epub")


if __name__ == "__main__":
    app()
