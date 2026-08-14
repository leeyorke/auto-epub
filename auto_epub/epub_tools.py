"""
EPUB 文件处理工具
"""

import re
from html import escape
from typing import Dict, List, Tuple

import tiktoken
from bs4 import BeautifulSoup, NavigableString, Tag
from ebooklib import ITEM_DOCUMENT, ITEM_IMAGE, epub

from .logger import get_logger
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
            get_logger().error(f"文档解码失败 ({item.get_name()}): {type(e).__name__}")
            return False
        else:
            # 排除没有 body 标签的文档
            if "<body" not in content:
                return False
            # 排除目录页（导航文档由 find_nav_documents 单独处理）
            if 'type="toc"' in content or 'epub:type="toc"' in content:
                return False
            return True

    @staticmethod
    def find_nav_documents(book: epub.EpubBook) -> List[epub.EpubHtml]:
        """找出 EPUB3 导航文档（阅读器侧边栏目录的来源）。

        这类文件不能靠 ebooklib 的 EpubNav 类型识别：calibre 等工具导出的
        EPUB 常常没在 OPF 里标 properties="nav"，读进来只是普通 EpubHtml，
        写盘时原样带回去，于是侧边栏永远是原文。改为按内容判断。
        """
        navs = []
        for item in book.get_items_of_type(ITEM_DOCUMENT):
            if isinstance(item, epub.EpubNav):
                navs.append(item)
                continue
            data = item.get_content()
            if not data:
                continue
            content = data.decode("utf-8", errors="ignore")
            if 'epub:type="toc"' in content or 'type="toc"' in content:
                navs.append(item)
        return navs

    @staticmethod
    def extract_nav_labels(html_content: str) -> List[str]:
        """提取导航文档中 toc 导航区的可见文本（按文档顺序）。

        只取 epub:type="toc" 的 nav：page-list 是页码、landmarks 是
        阅读器地标，翻译它们没有意义，还会打乱与正文目录的条目对应。
        """
        soup = BeautifulSoup(html_content, "xml")
        labels = []
        for nav in EpubTools._toc_navs(soup):
            for anchor in nav.find_all("a"):
                labels.append(anchor.get_text(strip=True))
        return labels

    @staticmethod
    def _toc_navs(soup: BeautifulSoup) -> List[Tag]:
        """取出所有 epub:type="toc" 的 nav 元素"""
        navs = []
        for nav in soup.find_all("nav"):
            nav_type = nav.get("epub:type") or nav.get("type")
            if nav_type and "toc" in nav_type:
                navs.append(nav)
        return navs

    @staticmethod
    def apply_nav_labels(html_content: str, mapping: Dict[str, str]) -> Tuple[str, int]:
        """按 {原文标题: 译文标题} 替换导航文档里的链接文字。

        用 xml 解析器：html.parser 会把 XHTML 的自闭合 <head> 内容丢掉，
        写回去的 nav.xhtml 会缺 <title> 和样式表链接。

        只替换 <a> 内最深一层的文本节点，保留 <i>/<span> 等内层标签，
        避免破坏原有排版。

        Returns:
            (新的 HTML, 实际替换条数)
        """
        soup = BeautifulSoup(html_content, "xml")
        replaced = 0

        for nav in EpubTools._toc_navs(soup):
            for anchor in nav.find_all("a"):
                original = anchor.get_text(strip=True)
                translated = mapping.get(original)
                if not translated or translated == original:
                    continue

                texts = [
                    node
                    for node in anchor.descendants
                    if isinstance(node, NavigableString) and node.strip()
                ]
                if not texts:
                    continue

                # 译文整体写进第一个文本节点，其余清空：
                # 标题被内层标签拆成多段时，按段分配会切错位置
                texts[0].replace_with(translated)
                for extra in texts[1:]:
                    extra.replace_with("")
                replaced += 1

        return str(soup), replaced

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
    def _open_tag(tag: Tag) -> str:
        """重建标签的起始部分，保留全部属性"""
        parts = [tag.name]
        for key, value in tag.attrs.items():
            if isinstance(value, list):  # class 等多值属性在 bs4 中是列表
                value = " ".join(value)
            if value is None:
                parts.append(key)
            else:
                parts.append(f'{key}="{escape(str(value), quote=True)}"')
        return f"<{' '.join(parts)}>"

    @staticmethod
    def _split_long_text(text: str, max_tokens: int) -> List[str]:
        """按句子边界切分过长的纯文本节点（无标签可供下钻时的兜底）"""
        pieces = re.split(r"(?<=[.!?。！？；;])\s+", text)
        out: List[str] = []
        buf = ""
        for piece in pieces:
            candidate = f"{buf} {piece}" if buf else piece
            if buf and EpubTools.count_tokens(candidate) > max_tokens:
                out.append(buf)
                buf = piece
            else:
                buf = candidate
        if buf:
            out.append(buf)
        return out or [text]

    @staticmethod
    def _atomize(node: Tag, max_tokens: int) -> List[str]:
        """把节点的子元素拆成原子片段，拼接后与原文完全一致。

        超过 max_tokens 的元素会下钻到其子元素：先产出起始标签，
        再递归处理内部，最后产出结束标签。这样单个 <section> 包裹
        整章内容时也能切开。
        """
        atoms: List[str] = []
        for child in node.children:
            child_str = str(child)
            if not child_str:
                continue
            if EpubTools.count_tokens(child_str) <= max_tokens:
                atoms.append(child_str)
                continue

            if isinstance(child, Tag) and any(
                isinstance(c, Tag) for c in child.children
            ):
                atoms.append(EpubTools._open_tag(child))
                atoms.extend(EpubTools._atomize(child, max_tokens))
                atoms.append(f"</{child.name}>")
            else:
                # 无子标签可下钻：按句子切开，标签本身留在首尾片段上
                atoms.extend(EpubTools._split_long_text(child_str, max_tokens))
        return atoms

    @staticmethod
    def split_html_content(
        html_content: str, max_tokens: int = INPUT_MAX_TOKENS
    ) -> List[str]:
        """
        将 HTML 内容分割成小块（避免超过 token 限制）
        保持 HTML 标签完整性，所有块拼接后与原 body 内容一致
        """
        soup = BeautifulSoup(html_content, "html.parser")
        body = soup.find("body")
        root = body if isinstance(body, Tag) else soup

        atoms = EpubTools._atomize(root, max_tokens)
        if not atoms:
            return [html_content]

        chunks: List[str] = []
        current: List[str] = []
        current_tokens = 0

        for atom in atoms:
            atom_tokens = EpubTools.count_tokens(atom)
            if current and current_tokens + atom_tokens > max_tokens:
                chunks.append("".join(current))
                current = []
                current_tokens = 0
            current.append(atom)
            current_tokens += atom_tokens

        if current:
            chunks.append("".join(current))

        # 丢弃只含空白的块，避免产生 1 个字符的无效分块
        return [c for c in chunks if c.strip()] or [html_content]
