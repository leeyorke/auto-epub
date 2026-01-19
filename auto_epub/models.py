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

    source_lang: str
    target_lang: str
    total_chapters: int
    completed_chapters: List[str] = Field(default_factory=list)
    failed_chapters: List[str] = Field(default_factory=list)
    glossary: Dict[str, str] = Field(default_factory=dict)
    toc_translated: bool = False
    images_translated: Dict[str, bool] = Field(default_factory=dict)
