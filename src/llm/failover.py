"""主备 fallback 链：把多个 LLMClient 按 priority 串成一条调用内故障转移链。

语义（调用内 fallback，用户确认）：
- 每次 ``decide_with_trace`` 依次试链里的源；某源在自身 ``max_retries`` 内仍失败
  （``trace.status == "degraded"``）→ 同一次调用内立即试下一源；
- 全部失败才返回最后一个 ``safe_hold``（HOLD），绝不带病下单；
- 备源是热备，每个决策都受保护；下个周期自动从主源重来（自动 failback）。

与 ``LLMClient`` 接口完全一致（decide / decide_with_trace / close），engine 把它当
``self._llm`` 用，决策调用点与 ``_llm_lock`` / version / audit 机制零改动。
"""
from __future__ import annotations

import json

from loguru import logger

from src.llm.client import LLMClient, LLMTrace
from src.llm.schema import MarketContext, TradeDecision


class LLMFailoverClient:
    def __init__(self, chain: list[tuple[str, LLMClient]]):
        # chain 已按 priority 升序排好；chain[0] 为主源/链头。
        self._chain = chain

    @property
    def primary_name(self) -> str:
        return self._chain[0][0] if self._chain else ""

    @property
    def source_names(self) -> list[str]:
        return [name for name, _ in self._chain]

    async def decide_with_trace(self, ctx: MarketContext) -> tuple[TradeDecision, LLMTrace]:
        if not self._chain:
            decision = TradeDecision.safe_hold(ctx.symbol, "no llm source in chain")
            trace = LLMTrace(user_prompt="", request_json="", status="degraded",
                             error="no llm source in chain")
            return decision, trace

        records: list[dict] = []
        last: tuple[TradeDecision, LLMTrace] | None = None
        for idx, (name, client) in enumerate(self._chain):
            decision, trace = await client.decide_with_trace(ctx)
            last = (decision, trace)
            records.append({"source": name, "status": trace.status, "error": trace.error})
            if trace.status == "ok":
                trace.source_name = name
                trace.fallback_used = idx > 0
                _annotate(trace, records)
                if idx > 0:
                    logger.warning(
                        "LLM failover ok: {} served by 备源 {} (前序失败: {})",
                        ctx.symbol, name, records[:-1],
                    )
                return decision, trace
            if idx + 1 < len(self._chain):
                logger.warning(
                    "LLM source {} degraded ({}), 尝试下一源", name, trace.error,
                )

        # 全链失败 → 返回最后一个 safe_hold，trace 标注全链失败。
        decision, trace = last  # type: ignore[misc]
        trace.source_name = ""
        trace.fallback_used = len(self._chain) > 1
        trace.status = "degraded"
        trace.error = (
            f"all {len(self._chain)} sources failed: "
            + "; ".join(f"{r['source']}:{r['error']}" for r in records)
        )[:200]
        _annotate(trace, records)
        logger.error("LLM all sources failed for {}: {}", ctx.symbol, records)
        return decision, trace

    async def decide(self, ctx: MarketContext) -> TradeDecision:
        decision, _ = await self.decide_with_trace(ctx)
        return decision

    async def close(self) -> None:
        for name, client in self._chain:
            try:
                await client.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("close llm source {} failed: {}", name, e)


def _annotate(trace: LLMTrace, records: list[dict]) -> None:
    """把 failover 链路信息塞进 response_json，便于决策详情面板展示兜底情况。"""
    try:
        obj = json.loads(trace.response_json) if trace.response_json else {}
    except Exception:  # noqa: BLE001
        obj = {"raw": trace.response_json}
    obj["failover"] = {
        "source": trace.source_name,
        "fallback_used": trace.fallback_used,
        "chain": records,
    }
    trace.response_json = json.dumps(obj, ensure_ascii=False, default=str)
