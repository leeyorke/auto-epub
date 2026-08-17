"""
数据模型定义
"""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class TranslationResult(BaseModel):
    """单次翻译结果"""

    translated_text: str
    new_terms: Dict[str, str] = Field(default_factory=dict)


class ChapterTranslation(BaseModel):
    """章节翻译结果"""

    chapter_id: str
    title: str
    original_content: str
    translated_content: str
    status: str = "pending"  # pending, completed, failed


class ImageTranslationResult(BaseModel):
    """图片翻译结果"""

    has_text: bool
    original_texts: List[str] = Field(default_factory=list)
    translated_texts: List[str] = Field(default_factory=list)
    new_image_base64: Optional[str] = None


class TranslationProgress(BaseModel):
    """翻译进度（用于缓存）"""

    # 缓存文件名是"绝对路径+目标语言"的 MD5，光看文件名认不出是哪本书，
    # 因此把书名（EPUB 文件名去后缀，与日志文件同名）记在第一个键上。
    # 默认空字符串是为了兼容没有这个字段的旧缓存。
    book_name: str = ""
    source_lang: str
    target_lang: str
    total_chapters: int
    completed_chapters: List[str] = Field(default_factory=list)
    failed_chapters: List[str] = Field(default_factory=list)
    glossary: Dict[str, str] = Field(default_factory=dict)
    toc_translated: bool = False
    # 译后的目录标题（按 book.toc 的递归顺序）。必须缓存：book.toc 每次
    # 都从原始 EPUB 重新读出，续译时若只看 toc_translated 就跳过，
    # 产出的 toc.ncx 会退回原文。
    toc_titles: List[str] = Field(default_factory=list)
    images_translated: Dict[str, bool] = Field(default_factory=dict)
