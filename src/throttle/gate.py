"""行情变化门槛：判断本周期是否需要调用 LLM。

核心是 ``should_call_llm`` —— 一个**纯函数**（无 IO、无副作用），
所有阈值都由调用方从 config 传入，便于 pytest 全覆盖。

触发 LLM 的任一条件（满足其一即触发）：
1. 首次决策（该 symbol 还没有 last_decision_price）
2. 价格相对上次决策价的变动绝对值 ≥ price_change_pct
3. 当前有持仓，且未实现盈亏绝对值触及 pnl_alert_pct
4. 有挂单成交 / 状态变化等关键事件（且 trigger_on_order_event 开启）
5. 连续跳过次数达到 max_skip_cycles（兜底：横盘时也强制决策一次）
"""
from __future__ import annotations

from dataclasses import dataclass

from src.llm.schema import PositionSnapshot

# 浮点容差：避免 0.3% 这类边界值因二进制误差被判为未达标
_EPS = 1e-9


@dataclass(frozen=True)
class ThrottleResult:
    trigger: bool
    reason: str


def should_call_llm(
    *,
    symbol: str,
    last_price: float,
    last_decision_px: float | None,
    position: PositionSnapshot,
    price_change_pct: float,
    pnl_alert_pct: float,
    order_event: bool,
    trigger_on_order_event: bool,
    skip_count: int,
    max_skip_cycles: int,
) -> ThrottleResult:
    """纯函数：返回是否触发 LLM 及原因。

    参数全部显式传入，不读全局状态。``last_decision_px=None`` 表示该 symbol
    尚无历史决策（首次），直接触发。
    """
    # 1. 首次决策
    if last_decision_px is None:
        return ThrottleResult(True, "first decision for symbol")

    # 2. 价格变动门槛
    if last_decision_px > 0:
        change_pct = abs(last_price - last_decision_px) / last_decision_px * 100.0
        if change_pct >= price_change_pct - _EPS:
            return ThrottleResult(
                True, f"price moved {change_pct:.3f}% >= {price_change_pct}%"
            )

    # 3. 持仓盈亏预警
    if position.has_position and position.unrealized_pnl_pct is not None:
        if abs(position.unrealized_pnl_pct) >= pnl_alert_pct - _EPS:
            return ThrottleResult(
                True,
                f"pnl {position.unrealized_pnl_pct:.3f}% hit alert {pnl_alert_pct}%",
            )

    # 4. 挂单事件
    if trigger_on_order_event and order_event:
        return ThrottleResult(True, "order event (fill/status change)")

    # 5. 兜底强制触发：连续跳过达到上限
    #    skip_count 是"在本次之前已经连续跳过的次数"。若再跳过就会变成 skip_count+1，
    #    当它将达到 max_skip_cycles 时强制触发。
    if skip_count + 1 >= max_skip_cycles:
        return ThrottleResult(
            True, f"max_skip_cycles reached (skipped {skip_count}, force decision)"
        )

    # 否则跳过
    return ThrottleResult(False, "no significant change")
