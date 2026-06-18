"""OpenAI 兼容 provider（chat/completions + function calling 强制 submit_decision）。

适配官方 OpenAI 及任何 OpenAI 兼容网关（base_url 指向第三方/自建）。
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger
from openai import AsyncOpenAI
from pydantic import ValidationError

from src.llm.providers._schema import _TOOL_NAME, build_openai_function
from src.llm.schema import TradeDecision


class OpenAICompatProvider:
    name = "openai_compatible"

    def __init__(self, *, model: str, base_url: str | None, api_key: str, timeout: float):
        self.model = model
        # 多数 openai_compatible 场景 base_url 必填（第三方/自建网关）；留空则走官方 OpenAI。
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**kwargs)
        self._fn = build_openai_function()

    def request_payload(self, *, system: str, user_prompt: str, max_tokens: int) -> dict:
        return {
            "model": self.model,
            "max_completion_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            "tools": [self._fn],
            "tool_choice": {"type": "function", "function": {"name": _TOOL_NAME}},
        }

    async def create(self, *, system: str, user_prompt: str, max_tokens: int) -> Any:
        return await self._client.chat.completions.create(
            **self.request_payload(system=system, user_prompt=user_prompt, max_tokens=max_tokens)
        )

    def parse(self, resp: Any, expected_symbol: str) -> TradeDecision | None:
        try:
            tool_calls = resp.choices[0].message.tool_calls
            if not tool_calls:
                logger.warning("openai resp has no tool_calls {}", expected_symbol)
                return None
            args = tool_calls[0].function.arguments  # JSON 字符串
            data = json.loads(args)
            return TradeDecision.model_validate(data)
        except (IndexError, AttributeError, json.JSONDecodeError, ValidationError) as e:
            logger.warning("openai decision parse failed {}: {}", expected_symbol, e)
            return None

    async def ping(self, *, max_tokens: int = 16) -> None:
        await self._client.chat.completions.create(
            model=self.model,
            max_completion_tokens=max_tokens,
            messages=[{"role": "user", "content": "ping"}],
        )

    async def close(self) -> None:
        await self._client.close()
