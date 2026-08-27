"""Tests for client.py - Agent 创建工厂与工具集清点（不联网）。"""

from pydantic_ai import Agent

from auto_epub.agent_tools import epub_toolset
from auto_epub.client import (
    create_chunk_agent,
    create_epub_agent,
    create_translator,
)
from auto_epub.settings import AGENT_SYSTEM_PROMPT, CHUNK_SYSTEM_PROMPT


class TestCreateEpubAgent:
    def test_returns_agent_named_for_toc_stage(self):
        agent = create_epub_agent("zh")
        assert isinstance(agent, Agent)
        assert agent.name == "epub_translator"

    def test_system_prompt_injects_target_language(self):
        agent = create_epub_agent("zh")
        prompts = "\n".join(agent._system_prompts)
        # 占位符必须被真实替换，且目标语言出现在提示词里
        assert "{target_language}" not in prompts
        assert "zh" in prompts


class TestCreateChunkAgent:
    def test_returns_agent_named_chunk_translator(self):
        agent = create_chunk_agent("ja")
        assert isinstance(agent, Agent)
        assert agent.name == "chunk_translator"

    def test_system_prompt_injects_target_language(self):
        agent = create_chunk_agent("日语")
        prompts = "\n".join(agent._system_prompts)
        assert "{target_language}" not in prompts
        assert "日语" in prompts


class TestToolsetInventory:
    """工具集只剩目录与图片两个阶段的工具；章节正文工具必须保持下线。"""

    EXPECTED_TOOLS = {
        "get_book_info",
        "list_chapters",
        "update_glossary",
        "get_glossary",
        "get_translation_progress",
        "translate_toc",
        "save_translated_toc",
        "list_images",
        "get_image_base64",
        "save_translated_image",
    }

    FORBIDDEN_CHAPTER_TOOLS = {
        "get_untranslated_content",
        "store_translation_chunk",
        "save_translated_chapter",
        "check_chapter_progress",
    }

    def test_all_expected_tools_registered(self):
        assert set(epub_toolset.tools) >= self.EXPECTED_TOOLS

    def test_no_chapter_tools_resurrected(self):
        """块级 run 下线了章节工具——重新出现说明有人回退了架构。"""
        assert not (set(epub_toolset.tools) & self.FORBIDDEN_CHAPTER_TOOLS)


class TestPromptsSource:
    def test_two_prompts_are_distinct_and_substantial(self):
        assert AGENT_SYSTEM_PROMPT != CHUNK_SYSTEM_PROMPT
        assert len(CHUNK_SYSTEM_PROMPT) > 1000
        assert len(AGENT_SYSTEM_PROMPT) > 1000


class TestCreateTranslator:
    def test_assembles_translator_with_both_agents(self):
        translator = create_translator("zh", cache_enabled=False)
        assert isinstance(translator.agent, Agent)
        assert isinstance(translator.chunk_agent, Agent)
        assert translator.cache_manager is None
