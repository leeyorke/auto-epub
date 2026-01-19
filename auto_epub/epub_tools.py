"""
EPUB 文件处理工具
"""

from typing import List

import tiktoken
from bs4 import BeautifulSoup
from ebooklib import ITEM_DOCUMENT, ITEM_IMAGE, epub

from .settings import INPUT_MAX_TOKENS


class EpubTools:
    """EPUB 文件操作工具集"""

    @staticmethod
    def get_default_language(book: epub.EpubBook) -> str:
        """获取 EPUB 默认语言"""
        metadata = book.get_metadata("DC", "language")
        if metadata and len(metadata) > 0:
            return metadata[0][0]
        return "en"

    @staticmethod
    def set_language(book: epub.EpubBook, language: str) -> None:
        """设置 EPUB 语言"""
        # 清除现有语言设置
        for data in book.metadata.values():
            if "language" in data:
                data["language"].clear()
        book.set_language(language)

    @staticmethod
    def get_all_chapters(book: epub.EpubBook) -> List[epub.EpubHtml]:
        """获取所有正文章节"""
        chapters = []
        for item in book.get_items_of_type(ITEM_DOCUMENT):
            if EpubTools._is_chapter(item):
                chapters.append(item)
        return chapters

    @staticmethod
    def get_all_images(book: epub.EpubBook) -> List[epub.EpubImage]:
        """获取所有图片"""
        return list(book.get_items_of_type(ITEM_IMAGE))

    @staticmethod
    def _is_chapter(item: epub.EpubHtml) -> bool:
        """判断是否为正文章节（排除目录等）"""
        data = item.get_content()
        if not data:
            return False

        try:
            content = data.decode("utf-8", errors="ignore")
        except Exception as e:
            print(f"❌ 错误 - {type(e).__name__}")
            return False
        else:
            # 排除没有 body 标签的文档
            if "<body" not in content:
                return False
            # 排除目录页
            if 'type="toc"' in content or 'epub:type="toc"' in content:
                return False
            return True

    @staticmethod
    def extract_text_from_html(html_content: str) -> str:
        """从 HTML 中提取纯文本（用于语言检测等）"""
        soup = BeautifulSoup(html_content, "html.parser")
        return soup.get_text(strip=True)

    @staticmethod
    def count_tokens(text: str, model: str = "gpt-4") -> int:
        """计算文本的 token 数量"""
        try:
            tokenizer = tiktoken.encoding_for_model(model)
            return len(tokenizer.encode(text))
        except Exception:
            # 备选方案：粗略估算（1 token ≈ 4 字符）
            return len(text) // 4

    @staticmethod
    def split_html_content(
        html_content: str, max_tokens: int = INPUT_MAX_TOKENS
    ) -> List[str]:
        """
        将 HTML 内容分割成小块（避免超过 token 限制）
        保持 HTML 标签完整性
        """
        soup = BeautifulSoup(html_content, "html.parser")
        body = soup.find("body")

        if not body:
            return [html_content]

        chunks = []
        current_chunk = []
        current_tokens = 0

        for element in body.children:
            element_str = str(element)
            element_tokens = EpubTools.count_tokens(element_str)

            if current_tokens + element_tokens > max_tokens and current_chunk:
                # 保存当前块
                chunks.append("".join(current_chunk))
                current_chunk = []
                current_tokens = 0

            current_chunk.append(element_str)
            current_tokens += element_tokens

        # 保存最后一块
        if current_chunk:
            chunks.append("".join(current_chunk))

        return chunks if chunks else [html_content]
