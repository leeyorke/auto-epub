"""
缓存管理器 - 支持断点续传
"""

import hashlib
import json
from pathlib import Path
from typing import Optional

from .models import TranslationProgress


class CacheManager:
    """翻译进度缓存管理"""

    def __init__(self, cache_dir: str = ".epub_translation_cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)

    def get_cache_key(self, epub_path: str, target_lang: str) -> str:
        """生成缓存键"""
        content = f"{Path(epub_path).absolute()}_{target_lang}"
        return hashlib.md5(content.encode()).hexdigest()

    def save_progress(self, cache_key: str, progress: TranslationProgress) -> None:
        """保存翻译进度"""
        cache_file = self.cache_dir / f"{cache_key}.json"
        cache_file.write_text(progress.model_dump_json(indent=2), encoding="utf-8")

    def load_progress(self, cache_key: str) -> Optional[TranslationProgress]:
        """加载翻译进度"""
        cache_file = self.cache_dir / f"{cache_key}.json"
        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                return TranslationProgress(**data)
            except Exception as e:
                print(f"警告: 加载缓存失败 - {e}")
                return None
        return None

    def save_chapter(self, cache_key: str, chapter_id: str, content: str) -> None:
        """保存单个章节翻译"""
        chapter_dir = self.cache_dir / cache_key / "chapters"
        chapter_dir.mkdir(parents=True, exist_ok=True)

        # 安全的文件名
        safe_id = hashlib.md5(chapter_id.encode()).hexdigest()
        chapter_file = chapter_dir / f"{safe_id}.html"
        chapter_file.write_text(content, encoding="utf-8")

    def load_chapter(self, cache_key: str, chapter_id: str) -> Optional[str]:
        """加载单个章节翻译"""
        safe_id = hashlib.md5(chapter_id.encode()).hexdigest()
        chapter_file = self.cache_dir / cache_key / "chapters" / f"{safe_id}.html"

        if chapter_file.exists():
            return chapter_file.read_text(encoding="utf-8")
        return None

    def save_image(self, cache_key: str, image_name: str, image_data: bytes) -> None:
        """保存翻译后的图片"""
        image_dir = self.cache_dir / cache_key / "images"
        image_dir.mkdir(parents=True, exist_ok=True)

        safe_name = hashlib.md5(image_name.encode()).hexdigest()
        image_file = image_dir / safe_name
        image_file.write_bytes(image_data)

    def load_image(self, cache_key: str, image_name: str) -> Optional[bytes]:
        """加载翻译后的图片"""
        safe_name = hashlib.md5(image_name.encode()).hexdigest()
        image_file = self.cache_dir / cache_key / "images" / safe_name

        if image_file.exists():
            return image_file.read_bytes()
        return None

    def clear_cache(self, cache_key: str) -> None:
        """清除指定缓存"""
        import shutil

        cache_path = self.cache_dir / cache_key
        if cache_path.exists():
            shutil.rmtree(cache_path)

        cache_file = self.cache_dir / f"{cache_key}.json"
        if cache_file.exists():
            cache_file.unlink()
