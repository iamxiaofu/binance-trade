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
    rt.update_equity(1000.0, now=0)
    assert rt.equity_peak == 1000.0
    assert rt.drawdown_pct == 0.0
    assert rt.risk_day_equity_peak == 1000.0
    assert rt.risk_day_drawdown_pct == 0.0
    rt.update_equity(900.0, now=0)
    assert rt.equity_peak == 1000.0
    assert abs(rt.drawdown_pct - 10.0) < 1e-9
    assert abs(rt.risk_day_drawdown_pct - 10.0) < 1e-9
    rt.update_equity(1100.0, now=0)  # 新高
    assert rt.equity_peak == 1100.0
    assert rt.drawdown_pct == 0.0
    assert rt.risk_day_equity_peak == 1100.0
    assert rt.risk_day_drawdown_pct == 0.0


def test_withdrawal_does_not_count_as_trading_drawdown():
    rt = RuntimeState()
    rt.update_equity(1000.0, now=0, net_capital_flow=0.0)
    rt.update_equity(700.0, now=0, net_capital_flow=-300.0)

    assert rt.current_equity == 700.0
    assert rt.risk_equity == 1000.0
    assert rt.risk_day_equity_peak == 1000.0
    assert rt.risk_day_drawdown_pct == 0.0
    assert rt.drawdown_pct == 30.0


def test_deposit_does_not_raise_risk_equity_peak():
    rt = RuntimeState()
    rt.update_equity(1000.0, now=0, net_capital_flow=0.0)
    rt.update_equity(1500.0, now=0, net_capital_flow=500.0)

    assert rt.risk_equity == 1000.0
    assert rt.risk_day_equity_peak == 1000.0
    assert rt.risk_day_drawdown_pct == 0.0


def test_withdrawal_and_real_loss_only_count_the_real_loss():
    rt = RuntimeState()
    rt.update_equity(1000.0, now=0, net_capital_flow=0.0)
    rt.update_equity(650.0, now=0, net_capital_flow=-300.0)

    assert rt.risk_equity == 950.0
    assert rt.risk_day_drawdown_pct == 5.0


def test_daily_drawdown_resets_and_bypass_expires_next_day():
    rt = RuntimeState()
    rt.update_equity(100.0, now=0)
    rt.update_equity(80.0, now=0)
    assert rt.risk_day_drawdown_pct == 20.0
    assert rt.grant_drawdown_bypass(now=0) == "1970-01-01"
    assert rt.drawdown_bypass_active(now=0) is True

    rt.update_equity(79.0, now=86400)

    assert rt.risk_day_key == "1970-01-02"
    assert rt.risk_day_equity_peak == 79.0
    assert rt.risk_day_drawdown_pct == 0.0
    assert rt.drawdown_bypass_active(now=86400) is False


def test_restore_daily_risk_ignores_stale_persisted_cycle():
    rt = RuntimeState()
    rt.restore_daily_risk(
        day_key="1999-01-01",
        equity_peak=999.0,
        bypass_day="1999-01-01",
        now=0,
    )
    assert rt.risk_day_key == "1970-01-01"
    assert rt.risk_day_equity_peak == 0.0
    assert rt.drawdown_bypass_day == ""


def test_breaker_and_kill_flags():
    rt = RuntimeState()
    assert not rt.halt_new_entries and not rt.kill_switch
    rt.trip_breaker("daily loss -21.00 <= -20.00")
    assert rt.halt_new_entries
    assert rt.halt_new_entries_reason == "circuit breaker: daily loss -21.00 <= -20.00"
    rt.resume_entries()
    assert not rt.halt_new_entries
    assert rt.halt_new_entries_reason == ""
    rt.trigger_kill()
    assert rt.halt_new_entries and rt.kill_switch
    assert rt.halt_new_entries_reason == "kill switch active"


def test_rehydrate_sets_day_key_to_today():
    """启动时 rehydrate 应当把 day_key 设到本地"今天"，并把今日 pnl 对齐到 by_day。"""
    import time as _t
    rt = RuntimeState()
    today = _t.strftime("%Y-%m-%d", _t.localtime())
    rt.rehydrate_day_pnl({today: -1.234})
    assert rt.day_key == today
    assert rt.day_realized_pnl == -1.234


def test_rehydrate_ignores_other_days_and_zero_today():
    rt = RuntimeState()
    rt.rehydrate_day_pnl({"2020-01-01": -999.0, "2099-12-31": 50.0})
    # 今日 by_day 缺失 → 0
    assert rt.day_realized_pnl == 0.0
    # day_key 仍初始化为本地今日
    import time as _t
    assert rt.day_key == _t.strftime("%Y-%m-%d", _t.localtime())


def test_rehydrate_empty_dict_only_inits_day_key():
    rt = RuntimeState()
    rt.rehydrate_day_pnl({})
    assert rt.day_realized_pnl == 0.0
    assert rt.day_key != ""


def test_day_roll_uses_local_timezone():
    """roll_day_if_needed 用本地时区（与本地 CST 容器一致）：now=UTC epoch 也按本地日界。"""
    import time as _t
    rt = RuntimeState()
    # epoch=0 (UTC 1970-01-01 00:00) → CST 1970-01-01 08:00 仍同一天，roll 不应触发
    rt.roll_day_if_needed(now=0)
    rt.add_realized_pnl(-5.0)
    assert rt.roll_day_if_needed(now=0) is False
    assert rt.day_realized_pnl == -5.0
    # 远未来时间（>1 天）应当 roll
    future = _t.time() + 86400 * 2
    assert rt.roll_day_if_needed(now=future) is True
    assert rt.day_realized_pnl == 0.0
