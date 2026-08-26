"""
缓存管理器 - 支持断点续传
"""

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Callable, Optional, Tuple

from .logger import get_logger
from .models import TranslationProgress
from .settings import CACHE_DIR, migrate_legacy_dir


class CacheManager:
    """翻译进度缓存管理

    进度文件是并发热点：pydantic-ai 会把同一个模型响应里的多个工具调用
    并发执行（同步工具跑在线程池里），而多个工具都要"读进度—改字段—写回"。
    因此这里的读写全部走同一把锁，写入用临时文件 + os.replace 保证原子。
    """

    def __init__(self, cache_dir=None):
        # 默认放 ~/.auto-epub/cache；工作目录里的旧版 .epub_translation_cache
        # 在首次运行时整体搬过去（见 settings.migrate_legacy_dir）
        self.cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        migrate_legacy_dir(".epub_translation_cache", self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def get_cache_key(self, epub_path: str, target_lang: str) -> str:
        """生成缓存键"""
        content = f"{Path(epub_path).absolute()}_{target_lang}"
        return hashlib.md5(content.encode()).hexdigest()

    def _progress_file(self, cache_key: str) -> Path:
        return self.cache_dir / f"{cache_key}.json"

    # ---------- 进度读写 ----------

    def save_progress(self, cache_key: str, progress: TranslationProgress) -> None:
        """保存翻译进度"""
        with self._lock:
            self._save_locked(cache_key, progress)

    def load_progress(self, cache_key: str) -> Optional[TranslationProgress]:
        """加载翻译进度"""
        with self._lock:
            return self._load_locked(cache_key)

    def update_progress(
        self, cache_key: str, mutate: Callable[[TranslationProgress], None]
    ) -> Optional[TranslationProgress]:
        """在锁内完成"读—改—写"，返回更新后的进度（无进度文件时返回 None）

        并发的工具调用若各自 load → 改 → save，后写的那个会拿自己读到的旧
        快照覆盖对方的改动（术语表就是这么丢的）。改动必须和读取在同一个
        临界区里，所以调用方把改动写成 mutate 回调传进来。
        """
        with self._lock:
            progress = self._load_locked(cache_key)
            if progress is None:
                return None
            mutate(progress)
            self._save_locked(cache_key, progress)
            return progress

    def _save_locked(self, cache_key: str, progress: TranslationProgress) -> None:
        """原子写入：先写同目录的临时文件，再整体替换

        直接 write_text 会先把原文件截断再写：两个写入者交错时，短文档只覆盖
        前半段，尾部残留上一版内容，读出来就是 "Extra data" 解析失败，
        之后整本书的进度都读不出来。
        """
        cache_file = self._progress_file(cache_key)
        tmp_file = cache_file.with_name(f"{cache_file.name}.{os.getpid()}.tmp")
        try:
            tmp_file.write_text(progress.model_dump_json(indent=2), encoding="utf-8")
            os.replace(tmp_file, cache_file)
        except OSError as e:
            get_logger().error(f"保存缓存失败: {e}")
            tmp_file.unlink(missing_ok=True)

    def _load_locked(self, cache_key: str) -> Optional[TranslationProgress]:
        cache_file = self._progress_file(cache_key)
        if not cache_file.exists():
            return None

        try:
            raw = cache_file.read_text(encoding="utf-8")
        except OSError as e:
            get_logger().error(f"读取缓存失败: {e}")
            return None

        data, has_trailing = self._decode(raw)
        if data is None:
            get_logger().error(f"加载缓存失败：内容不是合法 JSON（{cache_file}）")
            return None

        try:
            progress = TranslationProgress(**data)
        except Exception as e:
            get_logger().error(f"加载缓存失败: {e}")
            return None

        if has_trailing:
            # 非原子写入留下的尾部残留（旧版本的已知问题）：
            # 首个完整文档就是最后一次写入的内容，据此重写成干净文件
            get_logger().error(
                f"缓存文件有多余内容，已按首个完整 JSON 修复: {cache_file}"
            )
            self._save_locked(cache_key, progress)

        return progress

    @staticmethod
    def _decode(raw: str) -> Tuple[Optional[dict], bool]:
        """解析进度 JSON，容忍尾部残留。返回 (数据, 是否有残留)"""
        try:
            return json.loads(raw), False
        except json.JSONDecodeError:
            pass
        try:
            data, _ = json.JSONDecoder().raw_decode(raw.lstrip())
        except json.JSONDecodeError:
            return None, False
        return (data, True) if isinstance(data, dict) else (None, False)

    # ---------- 章节 ----------

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

    # ---------- 分块 ----------
    #
    # 分块缓存按「内容哈希」寻址，而不是块号：
    # INPUT_MAX_TOKENS 调整或原书更新导致切分变化时，哈希自然对不上 → 未命中
    # → 重译，既不需要版本字段，也不会把旧译文错位地贴到新块上。
    # 文件存在本身就是进度，所以块级不写进度文件（否则一章 37 次全量 JSON 重写）。

    def _chunk_file(self, cache_key: str, chapter_id: str, source: str) -> Path:
        safe_id = hashlib.md5(chapter_id.encode()).hexdigest()
        safe_src = hashlib.md5(source.encode("utf-8")).hexdigest()
        return self.cache_dir / cache_key / "chunks" / safe_id / f"{safe_src}.html"

    def save_chunk(
        self, cache_key: str, chapter_id: str, source: str, translated: str
    ) -> None:
        """保存单个分块的译文（只有通过校验的块才该调用这个）"""
        chunk_file = self._chunk_file(cache_key, chapter_id, source)
        try:
            chunk_file.parent.mkdir(parents=True, exist_ok=True)
            chunk_file.write_text(translated, encoding="utf-8")
        except OSError as e:
            get_logger().error(f"保存分块缓存失败: {e}")

    def load_chunk(self, cache_key: str, chapter_id: str, source: str) -> Optional[str]:
        """加载单个分块的译文，未命中返回 None"""
        chunk_file = self._chunk_file(cache_key, chapter_id, source)
        if not chunk_file.exists():
            return None
        try:
            return chunk_file.read_text(encoding="utf-8")
        except OSError as e:
            get_logger().error(f"读取分块缓存失败: {e}")
            return None

    # ---------- 图片 ----------

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

        with self._lock:
            cache_path = self.cache_dir / cache_key
            if cache_path.exists():
                shutil.rmtree(cache_path)

            cache_file = self._progress_file(cache_key)
            if cache_file.exists():
                cache_file.unlink()

    def clear_all(self) -> int:
        """清除全部翻译缓存，返回清除的书籍条目数

        缓存目录里每本书（更准确说是每个"书 + 语言"组合）占一个
        {md5}.json 进度文件和一个 {md5}/ 内容目录，全部清掉。
        """
        import shutil

        if not self.cache_dir.is_dir():
            return 0

        with self._lock:
            keys: set[str] = set()
            for entry in self.cache_dir.iterdir():
                # 进度文件名是 {md5}.json；残留的写入临时文件后缀不是 .json，
                # 名字长度也对不上 32 位十六进制，不会被计成一本书
                name = entry.stem if entry.suffix == ".json" else entry.name
                if len(name) == 32:
                    keys.add(name)
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
            return len(keys)
