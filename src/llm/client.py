"""Claude/OpenAI 兼容 决策客户端：强制结构化输出 + 超时/重试 → 失败降级 HOLD。

关键纪律：
- provider 层（``src.llm.providers``）用 tool-use / function-calling 强制 LLM 以
  ``submit_decision`` 工具返回，schema 来自 ``TradeDecision``，杜绝自由文本。
- 任何异常（超时、网络、解析失败、字段越界）都不抛给上层，统一返回
  ``TradeDecision.safe_hold(...)`` —— 绝不带病下单。
- LLM 永远拿不到任何密钥；client 只接收已构建好的 prompt。
- 单个 client 只对接「一个源」；主备故障转移由 ``LLMFailoverClient`` 在外层串接。
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from loguru import logger

from src.config.schema import LLMConfig
from src.llm.prompt import build_system_prompt, build_user_prompt
from src.llm.providers import build_provider
from src.llm.providers._schema import _TOOL_NAME  # noqa: F401  (向后兼容导出)
from src.llm.schema import MarketContext, TradeDecision


@dataclass
class LLMTrace:
    """一次 LLM 调用的审计信息，不包含 API key。"""

    user_prompt: str
    request_json: str
    system_prompt: str = ""
    response_json: str = ""
    # 调用耗时(毫秒)。包含所有 attempt 累计。0 表示未采集。
    latency_ms: int = 0
    # 实际请求次数(包含重试)。失败降级也会记录。
    attempts: int = 0
    # 最终状态: "ok" | "degraded"
    status: str = ""
    # 失败原因(成功时为空)。
    error: str = ""
    # 实际给出决策的对接源名（fallback 链里可能不是主源）。
    source_name: str = ""
    # 是否走了备源兜底（主源失败后由备源给出）。
    fallback_used: bool = False


def _jsonable(value: Any) -> Any:
    """Best-effort conversion for SDK response objects used in audit logs."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except TypeError:
            return value.model_dump()
    if isinstance(value, SimpleNamespace):
        return {k: _jsonable(v) for k, v in vars(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return repr(value)


def _dump_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, default=str)


class _LLMRuntime:
    """LLMClient 运行时视图：仅暴露 ``decide_with_trace`` 真正用到的字段。

    既能包成 LLMConfig（启动期）也能包成一个 profile（运行期热替换），
    避免在 LLMClient 内部做两套路径。
    """

    def __init__(self, *, provider, model, max_tokens, max_retries, timeout, base_url,
                 kline_interval, prompt_kline_count, micro_kline_lookback):
        self.provider = provider
        self.model = model
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.timeout = timeout
        self.base_url = base_url
        self.kline_interval = kline_interval
        self.prompt_kline_count = prompt_kline_count
        self.micro_kline_lookback = micro_kline_lookback

    @classmethod
    def from_config(cls, cfg: LLMConfig) -> "_LLMRuntime":
        return cls(
            provider=cfg.provider,
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            max_retries=cfg.max_retries,
            timeout=cfg.timeout,
            base_url=cfg.base_url,
            kline_interval=cfg.kline_interval,
            prompt_kline_count=cfg.prompt_kline_count,
            micro_kline_lookback=cfg.micro_kline_lookback,
        )

    @classmethod
    def from_profile(cls, profile: dict, engine_cfg: LLMConfig) -> "_LLMRuntime":
        """profile 来自 llm_profiles 表，``engine_cfg`` 提供不变的工程参数。"""
        return cls(
            provider=profile.get("provider") or "anthropic",
            model=profile["model"],
            max_tokens=int(profile["max_tokens"]),
            max_retries=int(profile["max_retries"]),
            timeout=float(profile["timeout"]),
            base_url=(profile.get("base_url") or None),
            kline_interval=engine_cfg.kline_interval,
            prompt_kline_count=engine_cfg.prompt_kline_count,
            micro_kline_lookback=engine_cfg.micro_kline_lookback,
        )


class LLMClient:
    def __init__(self, cfg: LLMConfig, api_key: str):
        self._cfg = _LLMRuntime.from_config(cfg)
        self._provider = build_provider(
            self._cfg.provider,
            model=self._cfg.model,
            base_url=self._cfg.base_url,
            api_key=api_key,
            timeout=self._cfg.timeout,
        )
        self._prompt_addendum = ""

    @classmethod
    def from_profile(
        cls, profile: dict, engine_cfg: LLMConfig, api_key: str
    ) -> "LLMClient":
        """从 profile dict + 工程 cfg 构造一个 LLMClient。

        profile 字段：provider/model/max_tokens/max_retries/timeout/base_url。
        工程 cfg（kline_interval 等）由 engine 透传，保证 prompt 结构不变。
        """
        obj = cls.__new__(cls)
        obj._cfg = _LLMRuntime.from_profile(profile, engine_cfg)
        obj._provider = build_provider(
            obj._cfg.provider,
            model=obj._cfg.model,
            base_url=obj._cfg.base_url,
            api_key=api_key,
            timeout=obj._cfg.timeout,
        )
        obj._prompt_addendum = str(profile.get("prompt_addendum") or "")
        return obj

    def set_prompt_addendum(self, addendum: str) -> None:
        self._prompt_addendum = addendum or ""

    async def decide(self, ctx: MarketContext) -> TradeDecision:
        """对单个 symbol 做决策。任何失败都降级为 HOLD。"""
        decision, _ = await self.decide_with_trace(ctx)
        return decision

    async def decide_with_trace(self, ctx: MarketContext) -> tuple[TradeDecision, LLMTrace]:
        """对单个 symbol 做决策，并返回完整审计 trace。"""
        user_prompt = build_user_prompt(
            ctx,
            kline_interval=self._cfg.kline_interval,
            prompt_kline_count=self._cfg.prompt_kline_count,
            micro_kline_count=self._cfg.micro_kline_lookback,
        )
        system_prompt = build_system_prompt(self._prompt_addendum)
        request_payload = self._provider.request_payload(
            system=system_prompt, user_prompt=user_prompt, max_tokens=self._cfg.max_tokens
        )
        trace = LLMTrace(
            user_prompt=user_prompt,
            request_json=_dump_json(request_payload),
            system_prompt=system_prompt,
        )
        last_err = "unknown"
        attempts: list[dict[str, Any]] = []
        loop_start = time.monotonic()

        for attempt in range(self._cfg.max_retries + 1):
            try:
                resp = await asyncio.wait_for(
                    self._provider.create(
                        system=system_prompt, user_prompt=user_prompt,
                        max_tokens=self._cfg.max_tokens,
                    ),
                    timeout=self._cfg.timeout,
                )
                attempts.append({"attempt": attempt + 1, "response": _jsonable(resp)})
                decision = self._parse(resp, ctx.symbol)
                if decision is not None:
                    trace.response_json = _dump_json({
                        "attempts": attempts,
                        "final_decision": decision.model_dump(mode="json"),
                    })
                    trace.latency_ms = int((time.monotonic() - loop_start) * 1000)
                    trace.attempts = attempt + 1
                    trace.status = "ok"
                    trace.error = ""
                    return decision, trace
                last_err = "no valid tool_use block"
            except asyncio.TimeoutError:
                last_err = f"timeout after {self._cfg.timeout}s"
                attempts.append({"attempt": attempt + 1, "error_type": "TimeoutError",
                                 "error": last_err})
                logger.warning("LLM timeout {} (attempt {})", ctx.symbol, attempt + 1)
            except Exception as e:  # 网络/限频/SDK 错误
                last_err = f"{type(e).__name__}: {e}"
                attempts.append({"attempt": attempt + 1, "error_type": type(e).__name__,
                                 "error": str(e)})
                logger.warning("LLM error {} (attempt {}): {}", ctx.symbol, attempt + 1, e)

            # 退避后重试
            if attempt < self._cfg.max_retries:
                await asyncio.sleep(min(2 ** attempt, 5))

        logger.error("LLM decide failed for {}, degrade HOLD: {}", ctx.symbol, last_err)
        decision = TradeDecision.safe_hold(ctx.symbol, last_err)
        trace.response_json = _dump_json({
            "attempts": attempts,
            "final_decision": decision.model_dump(mode="json"),
        })
        trace.latency_ms = int((time.monotonic() - loop_start) * 1000)
        trace.attempts = len(attempts)
        trace.status = "degraded"
        trace.error = last_err[:200]
        return decision, trace

    def _parse(self, resp, symbol: str) -> TradeDecision | None:
        """委托 provider 解析，并防止 LLM 把 symbol 写错。失败返回 None。"""
        decision = self._provider.parse(resp, symbol)
        if decision is None:
            return None
        if decision.symbol != symbol.upper():
            logger.warning(
                "LLM symbol mismatch {} vs {}, override", decision.symbol, symbol
            )
            decision = decision.model_copy(update={"symbol": symbol.upper()})
        return decision

    async def close(self) -> None:
        await self._provider.close()
