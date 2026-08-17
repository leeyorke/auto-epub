"""
Agent 客户端 - 使用 Toolsets 方式
"""

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from .agent_tools import EpubContext, epub_toolset
from .config import get_model_provider
from .settings import (
    AGENT_SYSTEM_PROMPT,
    ENABLE_CACHE,
    OUTPUT_MAX_TOKENS,
    TEMPERATURE,
    TIMEOUT,
)
from .translator import EpubTranslator


def create_epub_agent(target_language: str) -> Agent[EpubContext, str]:
    """
    创建 EPUB 翻译 Agent（包含工具集）

    Args:
        target_language: 目标语言代码

    Returns:
        配置好的 Agent
    """
    model_provider = get_model_provider()

    model = OpenAIChatModel(
        model_provider.model,
        provider=OpenAIProvider(
            base_url=model_provider.base_url, api_key=model_provider.api_key
        ),
        settings=OpenAIChatModelSettings(
            temperature=TEMPERATURE,
            # pydantic-ai 把 max_tokens 发成 max_completion_tokens
            max_tokens=OUTPUT_MAX_TOKENS,
            # 设置最低思考强度加快翻译速度
            openai_reasoning_effort="low",
            extra_body={
                # deepseek需要关闭思考模式，如启用，需要回传content
                "thinking": {"type": "disabled"},
                # 只认 max_tokens 的供应商（如 stepfun）走这条；
                # 两个字段都发出去，谁认哪个都能生效
                "max_tokens": OUTPUT_MAX_TOKENS,
            },
            timeout=TIMEOUT,
        ),
    )

    return Agent(
        model,
        name="epub_translator",
        system_prompt=AGENT_SYSTEM_PROMPT.format(target_language=target_language),
        deps_type=EpubContext,
        toolsets=[epub_toolset],  # 注册工具集
        retries=3,
    )


def create_translator(
    target_language: str, cache_enabled: bool = ENABLE_CACHE
) -> EpubTranslator:
    """
    创建 EPUB 翻译器

    Args:
        target_language: 目标语言代码
        cache_enabled: 是否启用缓存

    Returns:
        EpubTranslator 实例
    """
    agent = create_epub_agent(target_language)

    return EpubTranslator(agent=agent, cache_enabled=cache_enabled)
