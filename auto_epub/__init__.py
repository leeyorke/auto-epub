"""
EPUB Translator - 智能电子书翻译工具

基于 pydantic_ai 开发
支持并发翻译、断点续传、图片翻译等功能
"""

__version__ = "1.2.2"
__author__ = "leeyorke"

from .agent_tools import EpubContext, epub_toolset
from .client import create_epub_agent, create_translator
from .logger import ConsoleLevel, set_console_level
from .models import (
    ChapterTranslation,
    ImageTranslationResult,
    TranslationProgress,
    TranslationResult,
)
from .translator import EpubTranslator

__all__ = [
    "EpubTranslator",
    "create_translator",
    "create_epub_agent",
    "epub_toolset",
    "EpubContext",
    "ConsoleLevel",
    "set_console_level",
    "TranslationResult",
    "ImageTranslationResult",
    "TranslationProgress",
    "ChapterTranslation",
]
