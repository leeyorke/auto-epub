"""
Agent 客户端 - 使用 Toolsets 方式
"""

from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from .agent_tools import EpubContext, epub_toolset
from .config import get_model_provider
from .settings import (
    AGENT_SYSTEM_PROMPT,
    CHUNK_SYSTEM_PROMPT,
    ENABLE_CACHE,
    MAX_RETRIES,
    OUTPUT_MAX_TOKENS,
    TEMPERATURE,
    TIMEOUT,
)
from .translator import EpubTranslator


def _build_model() -> OpenAIChatModel:
    """两个 Agent 共用的模型配置（同一供应商、同一套 token 预算）

    单独抽出来是为了保证块级 Agent 和工具型 Agent 的 max_tokens / temperature
    完全一致：INPUT_MAX_TOKENS 的余量推导（见 settings.py）建立在
    OUTPUT_MAX_TOKENS 上，两边配置一旦漂移，块级译文会被静默截断。
    """
    model_provider = get_model_provider()

    return OpenAIChatModel(
        model_provider.model,
        provider=OpenAIProvider(
            # 显式建 client 只为接出 MAX_RETRIES：默认构造不暴露这个参数，
            # SDK 会按自己的默认值在幕后重试 2 次，把一次超时放大成
            # 3 × TIMEOUT 的墙钟且日志里只记一笔。HTTP 层不静默重试，
            # 失败一律落到块级循环里记账（取舍见 settings.MAX_RETRIES 注释）。
            openai_client=AsyncOpenAI(
                base_url=model_provider.base_url,
                api_key=model_provider.api_key,
                max_retries=MAX_RETRIES,
            ),
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


def create_epub_agent(target_language: str) -> Agent[EpubContext, str]:
    """
    创建工具型 Agent（目录与图片阶段专用）

    章节正文**不再走这个 Agent**：它带着 10 个工具的 schema，每个请求都要付一遍
    约 1.8k tokens，而块级翻译根本不需要工具（见 create_chunk_agent）。
    目录 / 图片本来就是各自一次独立 run，message history 不会跨章累积。

    Args:
        target_language: 目标语言代码

    Returns:
        配置好的 Agent
    """
    return Agent(
        _build_model(),
        name="epub_translator",
        system_prompt=AGENT_SYSTEM_PROMPT.format(target_language=target_language),
        deps_type=EpubContext,
        toolsets=[epub_toolset],  # 注册工具集
        retries=3,
    )


def create_chunk_agent(target_language: str) -> Agent[None, str]:
    """
    创建块级翻译 Agent：一块原文进，一块译文出，**没有任何工具**

    `toolsets=[]` 是这个 Agent 存在的全部理由。旧实现一章一次 run，模型靠
    get_untranslated_content / store_translation_chunk 循环取块存块，每块在
    message history 里留两份（原文 + 译文，约 9500 tokens）且永不丢弃，
    26 块就撞满 262144 的上下文窗口。改成每块一次独立 run 之后，单请求输入与
    块序号无关，章节长度不再有上限；顺带消灭了「工具调用参数 JSON 被截断 →
    模型退化成把 <tool_call> 当文本输出」这个最凶的故障模式。

    没有工具也就没有 deps：上下文靠 Python 拼进 prompt（chunk_translator
    的接力包），不靠 RunContext。

    Args:
        target_language: 目标语言代码

    Returns:
        配置好的无工具 Agent
    """
    return Agent(
        _build_model(),
        name="chunk_translator",
        system_prompt=CHUNK_SYSTEM_PROMPT.format(target_language=target_language),
        output_type=str,
        toolsets=[],
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
    return EpubTranslator(
        agent=create_epub_agent(target_language),
        chunk_agent=create_chunk_agent(target_language),
        cache_enabled=cache_enabled,
    )
