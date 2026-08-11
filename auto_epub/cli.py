"""
命令行接口
"""

import asyncio
import sys
from pathlib import Path

from typer import Argument, Option, Typer
from typing_extensions import Annotated

from . import __version__
from .client import create_translator
from .settings import DEBUG_MODE, ENABLE_CACHE, TRANSLATE_IMAGES, TRANSLATE_TOC


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
    """
    # 验证文件存在
    if not Path(file_path).exists():
        print(f"❌ 错误：文件不存在 - {file_path}")
        return

    if not file_path.endswith(".epub"):
        print(f"❌ 错误：不是 EPUB 文件 - {file_path}")
        return

    # 创建翻译器
    print("\n🚀 初始化翻译器...")
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

        print("\n🎉 翻译成功完成！")
        print(f"📁 输出文件：{output_file}")

    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断翻译")
        print("💡 提示：下次运行时使用 --resume 可以继续翻译")

    except Exception as e:
        print(f"\n❌ 翻译失败：{e}")
        import traceback

        if DEBUG_MODE:
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
