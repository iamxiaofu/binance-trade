"""最小回测/重放：用历史 K 线重放 + 同一套 throttle/risk 逻辑。

目的（SPEC 加分项）：验证「LLM 决策接口 + 风控夹断行为」，而非追求收益曲线。
- LLM 用可注入的 ``DecisionProvider``：可以是固定决策、随机、或从历史决策日志回放。
- 复用生产代码的 ``should_call_llm`` 与 ``risk.validate``，确保回测与实盘同一套闸门。
- 输出统计：触发/跳过次数、各拒单码计数、放行下单数，便于上线前核对风控行为。

这是可扩展骨架：后续可接 backtrader/vectorbt，只要替换 DecisionProvider 与撮合即可。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Protocol

from src.config.schema import Settings
from src.features.indicators import compute_snapshot
from src.llm.schema import (
    Action,
    IndicatorSnapshot,
    MarketContext,
    PositionSnapshot,
    TradeDecision,
)
from src.risk.manager import RiskContext, validate
from src.throttle.gate import should_call_llm


class DecisionProvider(Protocol):
    """给定 MarketContext 返回一个决策（模拟 LLM）。"""
    def __call__(self, ctx: MarketContext) -> TradeDecision: ...


def fixed_provider(decision_kwargs: dict) -> DecisionProvider:
    """总是返回同一套决策参数的 provider（便于测试风控夹断）。"""
    def _p(ctx: MarketContext) -> TradeDecision:
        kw = dict(decision_kwargs)
        kw.setdefault("symbol", ctx.symbol)
        return TradeDecision(**kw)
    return _p


@dataclass
class BacktestStats:
    cycles: int = 0
    triggered: int = 0
    skipped: int = 0
    passed: int = 0
    reject_codes: Counter = field(default_factory=Counter)
    decisions: list[TradeDecision] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "cycles": self.cycles,
            "triggered": self.triggered,
            "skipped": self.skipped,
            "passed_risk": self.passed,
            "rejects": dict(self.reject_codes),
        }


def replay(
    *,
    symbol: str,
    klines: list[list[float]],
    settings: Settings,
    provider: DecisionProvider,
    window: int = 50,
    available_margin: float = 200.0,
    price_at: Callable[[list[float]], float] | None = None,
) -> BacktestStats:
    """逐根重放 K 线。

    每根收盘视为一个周期：用截至当前的窗口算指标 → throttle 判定 →
    （触发则）provider 出决策 → risk.validate。统计触发/跳过/放行/拒单。
    不做撮合（无持仓演化），专注验证「决策→风控」链路的确定性行为。
    """
    stats = BacktestStats()
    price_of = price_at or (lambda k: float(k[4]))  # 默认用 close
    last_decision_px: float | None = None
    skip_count = 0
    empty_pos = PositionSnapshot(has_position=False)

    for i in range(window, len(klines)):
        stats.cycles += 1
        window_klines = klines[i - window : i + 1]
        last_price = price_of(klines[i])

        gate = should_call_llm(
            symbol=symbol,
            last_price=last_price,
            last_decision_px=last_decision_px,
            position=empty_pos,
            price_change_pct=settings.throttle.price_change_pct,
            pnl_alert_pct=settings.throttle.pnl_alert_pct,
            order_event=False,
            trigger_on_order_event=settings.throttle.trigger_on_order_event,
            skip_count=skip_count,
            max_skip_cycles=settings.throttle.max_skip_cycles,
        )
        if not gate.trigger:
            stats.skipped += 1
            skip_count += 1
            continue

        stats.triggered += 1
        skip_count = 0
        last_decision_px = last_price

        ind = compute_snapshot(window_klines)
        ctx = MarketContext(
            symbol=symbol,
            timestamp=int(klines[i][0]),
            last_price=last_price,
            mark_price=last_price,
            funding_rate=0.0,
            change_24h_pct=0.0,
            recent_klines=window_klines,
            indicators=IndicatorSnapshot(**ind),
            position=empty_pos,
            available_margin=available_margin,
            max_leverage_allowed=settings.risk.max_leverage,
        )
        decision = provider(ctx)
        stats.decisions.append(decision)

        if decision.action in (Action.HOLD, Action.CLOSE):
            continue

        rctx = RiskContext(last_price=last_price, available_margin=available_margin)
        verdict = validate(decision, rctx, settings)
        if verdict.passed:
            stats.passed += 1
        elif verdict.code is not None:
            stats.reject_codes[verdict.code.value] += 1

    return stats
