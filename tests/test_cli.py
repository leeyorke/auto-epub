"""Tests for cli.py - CLI 行为（不触网、不写真实缓存目录）。"""

from typer.testing import CliRunner

from auto_epub import __version__
from auto_epub.cli import app

runner = CliRunner()


class TestTranslateGuards:
    def test_quiet_and_verbose_mutually_exclusive(self):
        result = runner.invoke(app, ["translate", "book.epub", "-l", "zh", "-q", "-v"])
        assert result.exit_code == 1
        assert "不能同时使用" in result.output

    def test_rejects_non_epub_extension_before_any_io(self):
        # 用必然存在的当前目录当参数：存在性检查通过，随后卡在扩展名校验，
        # 全程不需要创建任何临时文件
        result = runner.invoke(app, ["translate", ".", "-l", "zh", "--no-resume"])
        assert result.exit_code == 1
        assert "不是 EPUB 文件" in result.output


class TestClearCommand:
    def test_clear_with_book_requires_language(self):
        result = runner.invoke(app, ["clear", "some.epub"])
        assert result.exit_code == 1
        assert "必须用 -l/--lang" in result.output


class TestVersionCommand:
    def test_prints_version_and_repo(self):
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert __version__ in result.output
