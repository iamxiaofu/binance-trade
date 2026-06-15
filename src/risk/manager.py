"""风控层：硬约束，优先级永远高于 LLM。

``validate`` 接收一个 ``TradeDecision`` + ``RiskContext``（运行态快照）+ Settings，
逐项校验。任一不过即返回 ``Verdict(passed=False, ...)``，调用方据此拒单。

关键铁律（来自需求）：
- LLM 返回 leverage > max_leverage → **直接拒单，不截断、不降级开仓**。
- 校验顺序：先全局闸门（熔断/日亏/kill），再单笔约束，最后强平距离。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from src.config.schema import Settings
from src.llm.schema import Action, TradeDecision


class RejectCode(str, Enum):
    HALT_NEW_ENTRIES = "HALT_NEW_ENTRIES"      # 运行态暂停新开仓
    DAILY_LOSS = "DAILY_LOSS"                  # 当日亏损达上限
    KILL_SWITCH = "KILL_SWITCH"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"          # 置信度不足，视为 HOLD
    LEVERAGE_EXCEEDED = "LEVERAGE_EXCEEDED"    # ★ 杠杆超 max_leverage
    ORDER_MARGIN = "ORDER_MARGIN"              # 单笔保证金占用超限
    SYMBOL_MARGIN = "SYMBOL_MARGIN"            # 单标的累计保证金超限
    TOTAL_MARGIN = "TOTAL_MARGIN"              # 全账户累计保证金超限
    TRADE_LOSS = "TRADE_LOSS"                  # 止损触发理论亏损超限
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    LIQ_DISTANCE = "LIQ_DISTANCE"              # 强平价距离过近
    INVALID_SIZE = "INVALID_SIZE"              # size_pct<=0 等
    STALE_CONDITION_ORDER = "STALE_CONDITION_ORDER"  # 交易所残留陈旧条件单
    STALE_MARKET_DATA = "STALE_MARKET_DATA"


@dataclass(frozen=True)
class Verdict:
    passed: bool
    code: RejectCode | None = None
    reason: str = ""
    # 通过时回填计算出的下单参数，供执行层使用
    notional: float = 0.0
    qty: float = 0.0

    @classmethod
    def ok(cls, notional: float, qty: float) -> "Verdict":
        return cls(passed=True, notional=notional, qty=qty)

    @classmethod
    def reject(cls, code: RejectCode, reason: str) -> "Verdict":
        return cls(passed=False, code=code, reason=reason)


@dataclass
class RiskContext:
    """风控所需的运行态快照，由 engine 组装传入。"""
    last_price: float                       # 当前价（用于估算名义价值/数量）
    available_margin: float                 # 可用保证金(USDT)
    equity: float = 0.0                     # 当前账户权益(USDT)，动态上限的计算基准
    symbol_position_margin: float = 0.0     # 该 symbol 当前持仓估算占用保证金
    total_open_margin: float = 0.0          # 全账户当前持仓估算占用保证金
    day_realized_pnl: float = 0.0           # 当日已实现盈亏(USDT，亏损为负)
    drawdown_pct: float = 0.0               # 当前账户回撤(%)
    halt_new_entries: bool = False          # 暂停新开仓后置 True
    halt_new_entries_reason: str = ""
    kill_switch: bool = False
    # 估算强平价用：维持保证金率，默认 0.5%（币安永续常见量级，可由交易所覆盖）
    maintenance_margin_rate: float = 0.005
    tags: dict = field(default_factory=dict)

    @property
    def equity_base(self) -> float:
        """名义价值/日亏上限的计算基准：优先用权益，缺失时退回可用保证金。"""
        return self.equity if self.equity > 0 else self.available_margin


def estimate_liq_distance_pct(
    *, side: Action, leverage: int, maintenance_margin_rate: float
) -> float:
    """估算强平价相对开仓价的距离百分比（逐仓近似）。

    逐仓下，多头强平价 ≈ entry * (1 - 1/lev + mmr)，
    故距离 ≈ (1/lev - mmr)。空头对称。返回正的百分比。
    leverage 越高，距离越近。
    """
    raw = (1.0 / leverage) - maintenance_margin_rate
    return max(raw, 0.0) * 100.0


def validate(
    decision: TradeDecision,
    ctx: RiskContext,
    settings: Settings,
) -> Verdict:
    """对单条决策做完整硬风控校验。

    仅对开仓(OPEN_LONG/OPEN_SHORT)做保证金/杠杆/止损亏损/强平距离等校验；
    CLOSE/HOLD 由调用方在更早阶段处理（CLOSE 平仓通常不受开仓限额约束）。
    """
    risk = settings.risk

    # ---- 0. 全局闸门（最高优先级）----
    if ctx.kill_switch:
        return Verdict.reject(RejectCode.KILL_SWITCH, "kill switch active")
    if ctx.halt_new_entries:
        reason = ctx.halt_new_entries_reason.strip()
        message = f"new entries halted: {reason}" if reason else "new entries halted"
        return Verdict.reject(RejectCode.HALT_NEW_ENTRIES, message)
    # 日亏限额 = 权益 × daily_max_loss_pct（随资金缩放）
    daily_max_loss = ctx.equity_base * (risk.daily_max_loss_pct / 100.0)
    if daily_max_loss > 0 and ctx.day_realized_pnl <= -abs(daily_max_loss):
        return Verdict.reject(
            RejectCode.DAILY_LOSS,
            f"daily loss {ctx.day_realized_pnl:.2f} <= -{daily_max_loss:.2f} "
            f"({risk.daily_max_loss_pct}% of {ctx.equity_base:.2f})",
        )

    # ---- 1. 置信度（低于阈值视为 HOLD，等价拒单不开仓）----
    if decision.confidence < risk.min_confidence:
        return Verdict.reject(
            RejectCode.LOW_CONFIDENCE,
            f"confidence {decision.confidence} < min {risk.min_confidence}",
        )

    # ---- 2. ★ 杠杆硬上限：超过即拒单，不截断 ----
    if decision.leverage > risk.max_leverage:
        return Verdict.reject(
            RejectCode.LEVERAGE_EXCEEDED,
            f"leverage {decision.leverage} > max_leverage {risk.max_leverage} (REJECT, no clamp)",
        )

    # ---- 3. 下单保证金、名义价值、数量计算 ----
    if decision.size_pct <= 0:
        return Verdict.reject(RejectCode.INVALID_SIZE, "size_pct <= 0")
    if ctx.available_margin <= 0:
        return Verdict.reject(RejectCode.INSUFFICIENT_MARGIN, "available_margin <= 0")
    if ctx.last_price <= 0:
        return Verdict.reject(RejectCode.INVALID_SIZE, "last_price <= 0")

    margin_to_use = ctx.available_margin * decision.size_pct
    notional = margin_to_use * decision.leverage  # 名义价值 = 保证金 × 杠杆
    qty = notional / ctx.last_price

    # ---- 4. 保证金三道上限（均按当前权益比例动态计算）----
    base = ctx.equity_base
    max_order_margin = base * risk.max_order_margin_pct
    max_symbol_margin = base * risk.max_symbol_margin_pct
    max_total_margin = base * risk.max_total_margin_pct
    if margin_to_use > max_order_margin:
        return Verdict.reject(
            RejectCode.ORDER_MARGIN,
            f"order margin {margin_to_use:.2f} > max {max_order_margin:.2f} "
            f"({risk.max_order_margin_pct*100:.0f}% of {base:.2f})",
        )
    if ctx.symbol_position_margin + margin_to_use > max_symbol_margin:
        return Verdict.reject(
            RejectCode.SYMBOL_MARGIN,
            f"symbol margin {ctx.symbol_position_margin + margin_to_use:.2f} "
            f"> max {max_symbol_margin:.2f}",
        )
    if ctx.total_open_margin + margin_to_use > max_total_margin:
        return Verdict.reject(
            RejectCode.TOTAL_MARGIN,
            f"total margin {ctx.total_open_margin + margin_to_use:.2f} "
            f"> max {max_total_margin:.2f}",
        )

    # ---- 5. 止损触发理论亏损上限（按本订单保证金）----
    if decision.stop_loss_pct <= 0:
        return Verdict.reject(
            RejectCode.TRADE_LOSS,
            "stop_loss_pct <= 0, cannot bound trade loss",
        )
    estimated_loss = notional * decision.stop_loss_pct
    max_loss = margin_to_use * (risk.max_loss_per_order_margin_pct / 100.0)
    if estimated_loss > max_loss:
        return Verdict.reject(
            RejectCode.TRADE_LOSS,
            f"estimated stop loss {estimated_loss:.2f} > max {max_loss:.2f} "
            f"({risk.max_loss_per_order_margin_pct}% of order margin {margin_to_use:.2f})",
        )

    # ---- 6. 强平价距离 ----
    liq_dist = estimate_liq_distance_pct(
        side=decision.action,
        leverage=decision.leverage,
        maintenance_margin_rate=ctx.maintenance_margin_rate,
    )
    if liq_dist < risk.liq_distance_min_pct:
        return Verdict.reject(
            RejectCode.LIQ_DISTANCE,
            f"liq distance {liq_dist:.2f}% < min {risk.liq_distance_min_pct}%",
        )

    return Verdict.ok(notional=notional, qty=qty)
