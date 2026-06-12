"""LLM provider 抽象与工厂。"""
from __future__ import annotations

from src.llm.providers.base import LLMProvider

__all__ = ["LLMProvider", "build_provider"]


def build_provider(
    provider: str,
    *,
    model: str,
    base_url: str | None,
    api_key: str,
    timeout: float,
) -> LLMProvider:
    """按 provider 名构造对应 provider；未知 provider 抛 ValueError。

    懒加载具体 provider 模块，避免在只用 anthropic 时强制 import openai SDK。
    """
    if provider == "anthropic":
        from src.llm.providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider(model=model, base_url=base_url, api_key=api_key, timeout=timeout)
    if provider == "openai_compatible":
        from src.llm.providers.openai_provider import OpenAICompatProvider
        return OpenAICompatProvider(model=model, base_url=base_url, api_key=api_key, timeout=timeout)
    raise ValueError(f"unknown llm provider: {provider!r}")
