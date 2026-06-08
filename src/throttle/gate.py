"""行情/状态变化门槛：判断本周期是否需要调用 LLM。

核心是 ``should_call_llm`` —— 一个**纯函数**（无 IO、无副作用），
所有阈值都由调用方从 config 传入，便于 pytest 全覆盖。

触发 LLM 的任一条件（满足其一即触发）：
1. 首次决策（该 symbol 还没有 last_decision_price）
2. 价格相对上次决策价的变动绝对值 ≥ price_change_pct
3. 当前有持仓，且未实现盈亏绝对值触及 pnl_alert_pct
4. 有挂单成交 / 状态变化等关键事件（且 trigger_on_order_event 开启）
5. 指标/多周期/微观K线状态相对上次决策快照发生关键变化
6. 动态最长复查间隔到期
7. 连续跳过次数达到 max_skip_cycles（最终兜底）
"""
from __future__ import annotations

from dataclasses import dataclass

from src.llm.schema import PositionSnapshot
from src.throttle.feature_snapshot import FeatureSnapshot

# 浮点容差：避免 0.3% 这类边界值因二进制误差被判为未达标
_EPS = 1e-9


@dataclass(frozen=True)
class ThrottleResult:
    trigger: bool
    reason: str


def _sign(value: float, eps: float = 0.0) -> int:
    if value > eps:
        return 1
    if value < -eps:
        return -1
    return 0


def _crossed(level: float, before: float, after: float) -> bool:
    return (before < level <= after) or (before > level >= after)


def _feature_change_reason(
    *,
    current: FeatureSnapshot | None,
    previous: FeatureSnapshot | None,
    ema_spread_cross_min_pct: float,
    macd_hist_cross_min_abs: float,
    rsi_midline: float,
    boll_bandwidth_low_pct: float,
    boll_bandwidth_expand_pct: float,
    volume_zscore_trigger: float,
    micro_return_5m_trigger_pct: float,
    micro_range_5m_trigger_pct: float,
) -> str | None:
    if current is None or previous is None:
        return None

    prev_ema = _sign(previous.ema_spread_pct)
    curr_ema = _sign(current.ema_spread_pct)
    if prev_ema != curr_ema and abs(current.ema_spread_pct) >= ema_spread_cross_min_pct:
        return (
            f"feature change: EMA spread sign {previous.ema_spread_pct:.4f}% "
            f"-> {current.ema_spread_pct:.4f}%"
        )

    prev_macd = _sign(previous.macd_hist, macd_hist_cross_min_abs)
    curr_macd = _sign(current.macd_hist, macd_hist_cross_min_abs)
    if prev_macd != curr_macd and curr_macd != 0:
        return f"feature change: MACD hist {previous.macd_hist:.6f} -> {current.macd_hist:.6f}"

    if _crossed(rsi_midline, previous.rsi, current.rsi):
        return f"feature change: RSI crossed {rsi_midline:g} ({previous.rsi:.2f}->{current.rsi:.2f})"

    expand_ratio = 1.0 + boll_bandwidth_expand_pct / 100.0
    if (
        previous.boll_bandwidth_pct <= boll_bandwidth_low_pct
        and current.boll_bandwidth_pct >= previous.boll_bandwidth_pct * expand_ratio
    ):
        return (
            "feature change: Bollinger bandwidth expanded "
            f"{previous.boll_bandwidth_pct:.4f}%->{current.boll_bandwidth_pct:.4f}%"
        )

    if (
        current.volume_zscore_20 >= volume_zscore_trigger
        and previous.volume_zscore_20 < volume_zscore_trigger
    ):
        return f"feature change: volume z-score {current.volume_zscore_20:.2f} >= {volume_zscore_trigger}"

    if current.trend_direction != previous.trend_direction and current.trend_direction != "flat":
        return f"feature change: trend {previous.trend_direction}->{current.trend_direction}"

    if current.higher_trends != previous.higher_trends:
        return "feature change: higher timeframe trend map changed"

    if (
        current.leader_symbol
        and current.leader_trend_direction
        and current.leader_trend_direction != previous.leader_trend_direction
    ):
        return (
            f"feature change: leader {current.leader_symbol} trend "
            f"{previous.leader_trend_direction}->{current.leader_trend_direction}"
        )

    if (
        current.leader_symbol
        and abs(current.leader_micro_return_5_pct) >= micro_return_5m_trigger_pct
        and abs(previous.leader_micro_return_5_pct) < micro_return_5m_trigger_pct
    ):
        return (
            f"feature change: leader {current.leader_symbol} 1m micro return 5m "
            f"{current.leader_micro_return_5_pct:.3f}% >= {micro_return_5m_trigger_pct}%"
        )

    if (
        current.leader_symbol
        and current.leader_volume_zscore_20 >= volume_zscore_trigger
        and previous.leader_volume_zscore_20 < volume_zscore_trigger
    ):
        return (
            f"feature change: leader {current.leader_symbol} volume z-score "
            f"{current.leader_volume_zscore_20:.2f} >= {volume_zscore_trigger}"
        )

    if (
        abs(current.micro_return_5_pct) >= micro_return_5m_trigger_pct
        and abs(previous.micro_return_5_pct) < micro_return_5m_trigger_pct
    ):
        return (
            f"feature change: 1m micro return 5m {current.micro_return_5_pct:.3f}% "
            f">= {micro_return_5m_trigger_pct}%"
        )

    if (
        current.micro_range_5_pct >= micro_range_5m_trigger_pct
        and previous.micro_range_5_pct < micro_range_5m_trigger_pct
    ):
        return (
            f"feature change: 1m micro range 5m {current.micro_range_5_pct:.3f}% "
            f">= {micro_range_5m_trigger_pct}%"
        )

    return None


def _review_interval_minutes(
    *,
    current: FeatureSnapshot | None,
    position: PositionSnapshot,
    review_flat_minutes: int,
    review_position_minutes: int,
    review_near_exit_minutes: int,
    review_high_vol_minutes: int,
    near_exit_pnl_pct: float,
    volume_zscore_trigger: float,
    micro_return_5m_trigger_pct: float,
    micro_range_5m_trigger_pct: float,
) -> int:
    if current is not None:
        high_vol = (
            current.volume_zscore_20 >= volume_zscore_trigger
            or abs(current.micro_return_5_pct) >= micro_return_5m_trigger_pct
            or current.micro_range_5_pct >= micro_range_5m_trigger_pct
        )
        if high_vol:
            return review_high_vol_minutes

    if position.has_position:
        pnl = position.unrealized_pnl_pct
        if pnl is not None and abs(pnl) >= near_exit_pnl_pct:
            return review_near_exit_minutes
        return review_position_minutes

    return review_flat_minutes


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
    last_decision_ts_ms: int | None = None,
    now_ts_ms: int | None = None,
    current_snapshot: FeatureSnapshot | None = None,
    last_decision_snapshot: FeatureSnapshot | None = None,
    feature_snapshot_enabled: bool = True,
    ema_spread_cross_min_pct: float = 0.02,
    macd_hist_cross_min_abs: float = 0.0,
    rsi_midline: float = 50.0,
    boll_bandwidth_low_pct: float = 1.0,
    boll_bandwidth_expand_pct: float = 25.0,
    volume_zscore_trigger: float = 2.0,
    micro_return_5m_trigger_pct: float = 0.5,
    micro_range_5m_trigger_pct: float = 0.8,
    near_exit_pnl_pct: float = 0.8,
    review_flat_minutes: int = 60,
    review_position_minutes: int = 15,
    review_near_exit_minutes: int = 5,
    review_high_vol_minutes: int = 5,
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

    # 5. 特征状态变化
    if feature_snapshot_enabled:
        reason = _feature_change_reason(
            current=current_snapshot,
            previous=last_decision_snapshot,
            ema_spread_cross_min_pct=ema_spread_cross_min_pct,
            macd_hist_cross_min_abs=macd_hist_cross_min_abs,
            rsi_midline=rsi_midline,
            boll_bandwidth_low_pct=boll_bandwidth_low_pct,
            boll_bandwidth_expand_pct=boll_bandwidth_expand_pct,
            volume_zscore_trigger=volume_zscore_trigger,
            micro_return_5m_trigger_pct=micro_return_5m_trigger_pct,
            micro_range_5m_trigger_pct=micro_range_5m_trigger_pct,
        )
        if reason:
            return ThrottleResult(True, reason)

    # 6. 动态最长复查间隔
    if last_decision_ts_ms is not None and now_ts_ms is not None:
        review_minutes = _review_interval_minutes(
            current=current_snapshot,
            position=position,
            review_flat_minutes=review_flat_minutes,
            review_position_minutes=review_position_minutes,
            review_near_exit_minutes=review_near_exit_minutes,
            review_high_vol_minutes=review_high_vol_minutes,
            near_exit_pnl_pct=near_exit_pnl_pct,
            volume_zscore_trigger=volume_zscore_trigger,
            micro_return_5m_trigger_pct=micro_return_5m_trigger_pct,
            micro_range_5m_trigger_pct=micro_range_5m_trigger_pct,
        )
        elapsed_ms = max(0, now_ts_ms - last_decision_ts_ms)
        if elapsed_ms >= review_minutes * 60_000 - _EPS:
            return ThrottleResult(
                True,
                f"dynamic review interval reached ({elapsed_ms / 60000:.1f}m >= {review_minutes}m)",
            )

    # 7. 兜底强制触发：连续跳过达到上限
    #    skip_count 是"在本次之前已经连续跳过的次数"。若再跳过就会变成 skip_count+1，
    #    当它将达到 max_skip_cycles 时强制触发。
    if skip_count + 1 >= max_skip_cycles:
        return ThrottleResult(
            True, f"max_skip_cycles reached (skipped {skip_count}, force decision)"
        )

    # 否则跳过
    return ThrottleResult(False, "no significant change")
