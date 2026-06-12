"""Anthropic Messages API provider（tool_use 强制 submit_decision）。"""
from __future__ import annotations

from typing import Any

from anthropic import AsyncAnthropic
from loguru import logger
from pydantic import ValidationError

from src.llm.prompt import SYSTEM_PROMPT  # noqa: F401  (保持与 client 一致的来源)
from src.llm.providers._schema import _TOOL_NAME, build_anthropic_tool
from src.llm.schema import TradeDecision


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, model: str, base_url: str | None, api_key: str, timeout: float):
        self.model = model
        # base_url 为空 → 官方 api.anthropic.com；否则指向 Anthropic 兼容中转端点。
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncAnthropic(**kwargs)
        self._tool = build_anthropic_tool()

    def request_payload(self, *, system: str, user_prompt: str, max_tokens: int) -> dict:
        return {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "tools": [self._tool],
            "tool_choice": {"type": "tool", "name": _TOOL_NAME},
            "messages": [{"role": "user", "content": user_prompt}],
        }

    async def create(self, *, system: str, user_prompt: str, max_tokens: int) -> Any:
        return await self._client.messages.create(
            **self.request_payload(system=system, user_prompt=user_prompt, max_tokens=max_tokens)
        )

    def parse(self, resp: Any, expected_symbol: str) -> TradeDecision | None:
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == _TOOL_NAME:
                try:
                    return TradeDecision.model_validate(block.input)
                except ValidationError as e:
                    logger.warning("anthropic decision validation failed {}: {}", expected_symbol, e)
                    return None
        return None

    async def ping(self, *, max_tokens: int = 16) -> None:
        await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": "ping"}],
        )

    async def close(self) -> None:
        await self._client.close()
