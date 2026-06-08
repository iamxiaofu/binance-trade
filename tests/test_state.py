"""state/runtime.py 测试。"""
from __future__ import annotations

from src.state.runtime import RuntimeState


def test_record_decision_resets_skip():
    rt = RuntimeState()
    rt.record_skip("BTCUSDT")
    rt.record_skip("BTCUSDT")
    assert rt.skip_count["BTCUSDT"] == 2
    rt.record_decision("BTCUSDT", 100.0, ts_ms=123, feature_snapshot={"trend": "up"})
    assert rt.skip_count["BTCUSDT"] == 0
    assert rt.last_decision_price["BTCUSDT"] == 100.0
    assert rt.last_decision_time["BTCUSDT"] == 123
    assert rt.last_decision_snapshot["BTCUSDT"] == {"trend": "up"}


def test_order_event_pop_is_one_shot():
    rt = RuntimeState()
    assert rt.pop_order_event("BTCUSDT") is False
    rt.mark_order_event("BTCUSDT")
    assert rt.pop_order_event("BTCUSDT") is True
    assert rt.pop_order_event("BTCUSDT") is False


def test_day_roll_resets_pnl():
    rt = RuntimeState()
    rt.day_key = "1999-01-01"
    rt.add_realized_pnl(-50.0)
    rolled = rt.roll_day_if_needed(now=0)  # epoch=1970-01-01 != 1999
    assert rolled is True
    assert rt.day_realized_pnl == 0.0


def test_day_roll_noop_same_day():
    rt = RuntimeState()
    rt.roll_day_if_needed(now=0)
    rt.add_realized_pnl(-10.0)
    rolled = rt.roll_day_if_needed(now=0)
    assert rolled is False
    assert rt.day_realized_pnl == -10.0


def test_equity_peak_and_drawdown():
    rt = RuntimeState()
    rt.update_equity(1000.0)
    assert rt.equity_peak == 1000.0
    assert rt.drawdown_pct == 0.0
    rt.update_equity(900.0)
    assert rt.equity_peak == 1000.0
    assert abs(rt.drawdown_pct - 10.0) < 1e-9
    rt.update_equity(1100.0)  # 新高
    assert rt.equity_peak == 1100.0
    assert rt.drawdown_pct == 0.0


def test_breaker_and_kill_flags():
    rt = RuntimeState()
    assert not rt.halt_new_entries and not rt.kill_switch
    rt.trip_breaker()
    rt.trigger_kill()
    assert rt.halt_new_entries and rt.kill_switch
