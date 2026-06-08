"""throttle 纯函数测试。"""
from __future__ import annotations

from src.llm.schema import PositionSnapshot
from src.throttle.feature_snapshot import FeatureSnapshot
from src.throttle.gate import should_call_llm


def _snap(**kw):
    base = dict(
        symbol="BTCUSDT",
        ts_ms=1,
        last_price=100.0,
        mark_price=100.0,
        trend_direction="flat",
        trend_score=0.0,
        ema_spread_pct=-0.05,
        macd_hist=-0.01,
        rsi=49.0,
        atr_pct=0.5,
        boll_bandwidth_pct=0.8,
        volume_ratio=1.0,
        volume_zscore_20=0.0,
        micro_return_5_pct=0.0,
        micro_range_5_pct=0.2,
    )
    base.update(kw)
    return FeatureSnapshot(**base)


def _call(**kw):
    base = dict(
        symbol="BTCUSDT",
        last_price=100.0,
        last_decision_px=100.0,
        position=PositionSnapshot(),
        price_change_pct=0.3,
        pnl_alert_pct=1.0,
        order_event=False,
        trigger_on_order_event=True,
        skip_count=0,
        max_skip_cycles=6,
    )
    base.update(kw)
    return should_call_llm(**base)


def test_first_decision_triggers():
    r = _call(last_decision_px=None)
    assert r.trigger is True
    assert "first" in r.reason


def test_price_change_below_threshold_skips():
    # 100 → 100.2 = 0.2% < 0.3%
    r = _call(last_price=100.2)
    assert r.trigger is False


def test_price_change_at_threshold_triggers():
    # 100 → 100.3 = 0.3% >= 0.3%
    r = _call(last_price=100.3)
    assert r.trigger is True
    assert "price moved" in r.reason


def test_price_drop_triggers():
    # 绝对值：100 → 99.6 = 0.4%
    r = _call(last_price=99.6)
    assert r.trigger is True


def test_pnl_alert_triggers():
    pos = PositionSnapshot(has_position=True, unrealized_pnl_pct=-1.5)
    r = _call(position=pos, last_price=100.05)  # 价格变动不足，靠盈亏触发
    assert r.trigger is True
    assert "pnl" in r.reason


def test_pnl_below_alert_skips():
    pos = PositionSnapshot(has_position=True, unrealized_pnl_pct=0.5)
    r = _call(position=pos, last_price=100.05)
    assert r.trigger is False


def test_order_event_triggers():
    r = _call(last_price=100.05, order_event=True)
    assert r.trigger is True
    assert "order event" in r.reason


def test_order_event_ignored_when_disabled():
    r = _call(last_price=100.05, order_event=True, trigger_on_order_event=False)
    assert r.trigger is False


def test_max_skip_cycles_force_trigger():
    # skip_count=5, max=6 → 5+1>=6 强制触发
    r = _call(last_price=100.05, skip_count=5, max_skip_cycles=6)
    assert r.trigger is True
    assert "max_skip_cycles" in r.reason


def test_below_max_skip_still_skips():
    r = _call(last_price=100.05, skip_count=3, max_skip_cycles=6)
    assert r.trigger is False


def test_no_change_skips():
    r = _call(last_price=100.0)
    assert r.trigger is False
    assert "no significant change" in r.reason


def test_feature_snapshot_ema_cross_triggers():
    prev = _snap(ema_spread_pct=-0.03)
    cur = _snap(ema_spread_pct=0.04)
    r = _call(current_snapshot=cur, last_decision_snapshot=prev, last_price=100.05)
    assert r.trigger is True
    assert "EMA spread" in r.reason


def test_feature_snapshot_volume_spike_triggers():
    prev = _snap(volume_zscore_20=0.5)
    cur = _snap(volume_zscore_20=2.5)
    r = _call(current_snapshot=cur, last_decision_snapshot=prev, last_price=100.05)
    assert r.trigger is True
    assert "volume z-score" in r.reason


def test_feature_snapshot_micro_move_triggers():
    prev = _snap(micro_return_5_pct=0.1)
    cur = _snap(micro_return_5_pct=0.8)
    r = _call(current_snapshot=cur, last_decision_snapshot=prev, last_price=100.05)
    assert r.trigger is True
    assert "micro return" in r.reason


def test_feature_snapshot_leader_micro_move_triggers():
    prev = _snap(symbol="ETHUSDT", leader_symbol="BTCUSDT", leader_micro_return_5_pct=0.1)
    cur = _snap(symbol="ETHUSDT", leader_symbol="BTCUSDT", leader_micro_return_5_pct=0.9)
    r = _call(symbol="ETHUSDT", current_snapshot=cur, last_decision_snapshot=prev,
              last_price=100.05)
    assert r.trigger is True
    assert "leader BTCUSDT" in r.reason


def test_dynamic_position_review_interval_triggers():
    pos = PositionSnapshot(has_position=True, unrealized_pnl_pct=0.2)
    r = _call(
        position=pos,
        current_snapshot=_snap(),
        last_decision_snapshot=_snap(),
        last_decision_ts_ms=0,
        now_ts_ms=16 * 60_000,
        last_price=100.05,
    )
    assert r.trigger is True
    assert "dynamic review" in r.reason
