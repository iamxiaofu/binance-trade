"""Claude API 决策客户端：强制结构化输出 + 超时/重试 → 失败降级 HOLD。

关键纪律：
- 用 Anthropic tool-use 强制 LLM 以 ``submit_decision`` 工具返回，schema 来自
  ``TradeDecision``，配合 ``tool_choice`` 强制调用，杜绝自由文本。
- 任何异常（超时、网络、解析失败、字段越界）都不抛给上层，统一返回
  ``TradeDecision.safe_hold(...)`` —— 绝不带病下单。
- LLM 永远拿不到任何密钥；client 只接收已构建好的 prompt。
"""
from __future__ import annotations

import asyncio

from anthropic import AsyncAnthropic
from loguru import logger
from pydantic import ValidationError

from src.config.schema import LLMConfig
from src.llm.prompt import SYSTEM_PROMPT, build_user_prompt
from src.llm.schema import MarketContext, TradeDecision

_TOOL_NAME = "submit_decision"


def _build_tool() -> dict:
    """用 TradeDecision 的 JSON Schema 构造 Anthropic tool 定义。"""
    schema = TradeDecision.model_json_schema()
    # Anthropic input_schema 需要 type=object；移除 pydantic 特有的 $defs 引用问题
    schema.pop("title", None)
    return {
        "name": _TOOL_NAME,
        "description": "提交本周期对该标的的结构化交易决策。必须调用本工具。",
        "input_schema": schema,
    }


class LLMClient:
    def __init__(self, cfg: LLMConfig, api_key: str):
        self._cfg = cfg
        # base_url 为空 → 官方 api.anthropic.com；否则指向 Anthropic 兼容中转端点。
        kwargs = {"api_key": api_key, "timeout": cfg.timeout}
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        self._client = AsyncAnthropic(**kwargs)
        self._tool = _build_tool()

    async def decide(self, ctx: MarketContext) -> TradeDecision:
        """对单个 symbol 做决策。任何失败都降级为 HOLD。"""
        user_prompt = build_user_prompt(ctx, kline_interval=self._cfg.kline_interval)
        last_err = "unknown"

        for attempt in range(self._cfg.max_retries + 1):
            try:
                resp = await asyncio.wait_for(
                    self._client.messages.create(
                        model=self._cfg.model,
                        max_tokens=self._cfg.max_tokens,
                        system=SYSTEM_PROMPT,
                        tools=[self._tool],
                        tool_choice={"type": "tool", "name": _TOOL_NAME},
                        messages=[{"role": "user", "content": user_prompt}],
                    ),
                    timeout=self._cfg.timeout,
                )
                decision = self._parse(resp, ctx.symbol)
                if decision is not None:
                    return decision
                last_err = "no valid tool_use block"
            except asyncio.TimeoutError:
                last_err = f"timeout after {self._cfg.timeout}s"
                logger.warning("LLM timeout {} (attempt {})", ctx.symbol, attempt + 1)
            except Exception as e:  # 网络/限频/SDK 错误
                last_err = f"{type(e).__name__}: {e}"
                logger.warning("LLM error {} (attempt {}): {}", ctx.symbol, attempt + 1, e)

            # 退避后重试
            if attempt < self._cfg.max_retries:
                await asyncio.sleep(min(2 ** attempt, 5))

        logger.error("LLM decide failed for {}, degrade HOLD: {}", ctx.symbol, last_err)
        return TradeDecision.safe_hold(ctx.symbol, last_err)

    def _parse(self, resp, symbol: str) -> TradeDecision | None:
        """从响应里取出 tool_use 输入并校验为 TradeDecision。失败返回 None。"""
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == _TOOL_NAME:
                try:
                    decision = TradeDecision.model_validate(block.input)
                    # 防止 LLM 把 symbol 写错
                    if decision.symbol != symbol.upper():
                        logger.warning(
                            "LLM symbol mismatch {} vs {}, override", decision.symbol, symbol
                        )
                        decision = decision.model_copy(update={"symbol": symbol.upper()})
                    return decision
                except ValidationError as e:
                    logger.warning("LLM decision validation failed {}: {}", symbol, e)
                    return None
        return None

    async def close(self) -> None:
        await self._client.close()
