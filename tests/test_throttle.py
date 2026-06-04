"""throttle 纯函数测试。"""
from __future__ import annotations

from src.llm.schema import PositionSnapshot
from src.throttle.gate import should_call_llm


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
