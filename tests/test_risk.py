"""风控测试：重点覆盖 leverage > max_leverage 必须拒单（铁律）。"""
from __future__ import annotations

import pytest

from src.llm.schema import Action, TradeDecision
from src.risk.manager import RejectCode, RiskContext, estimate_liq_distance_pct, validate


def _decision(**kw) -> TradeDecision:
    base = dict(
        symbol="BTCUSDT", action=Action.OPEN_LONG, confidence=0.9,
        size_pct=0.1, leverage=2, stop_loss_pct=0.02,
        take_profit_pct=0.04, reason="test",
    )
    base.update(kw)
    return TradeDecision(**base)


def _ctx(**kw) -> RiskContext:
    base = dict(last_price=100.0, available_margin=200.0)
    base.update(kw)
    return RiskContext(**base)


def test_leverage_exceeded_rejects_not_clamps(settings):
    """★ LLM 给 5 倍，max_leverage=3 → 必须拒单，且不截断。"""
    d = _decision(leverage=5)
    v = validate(d, _ctx(), settings)
    assert v.passed is False
    assert v.code is RejectCode.LEVERAGE_EXCEEDED
    assert "no clamp" in v.reason
    assert v.qty == 0.0  # 没有产出可下单数量


def test_leverage_at_limit_passes(settings):
    """等于上限 3 应放行。"""
    d = _decision(leverage=3, size_pct=0.1)  # notional=200*0.1*3=60 < 100
    v = validate(d, _ctx(), settings)
    assert v.passed is True
    assert v.notional == pytest.approx(60.0)


def test_low_confidence_rejected(settings):
    d = _decision(confidence=0.5)  # < 0.6
    v = validate(d, _ctx(), settings)
    assert v.passed is False
    assert v.code is RejectCode.LOW_CONFIDENCE


def test_order_margin_limit(settings):
    # equity_base=200, max_order_margin_pct=0.2 => max margin 40;
    # order margin = available_margin 200 * size_pct 0.5 = 100 > 40
    d = _decision(leverage=3, size_pct=0.5)
    v = validate(d, _ctx(), settings)
    assert v.passed is False
    assert v.code is RejectCode.ORDER_MARGIN


def test_symbol_margin_limit(settings):
    # 单笔 margin=20 合规，但叠加已有持仓保证金 70 → 90 > 80
    d = _decision(leverage=3, size_pct=0.1)
    v = validate(d, _ctx(symbol_position_margin=70.0), settings)
    assert v.passed is False
    assert v.code is RejectCode.SYMBOL_MARGIN


def test_total_margin_limit(settings):
    d = _decision(leverage=3, size_pct=0.1)  # margin=20
    v = validate(d, _ctx(total_open_margin=150.0), settings)
    assert v.passed is False
    assert v.code is RejectCode.TOTAL_MARGIN


def test_trade_loss_limit(settings):
    # margin=40, lev=3, notional=120, stop=5% => estimated loss=6 > max 4
    d = _decision(leverage=3, size_pct=0.2, stop_loss_pct=0.05)
    v = validate(d, _ctx(), settings)
    assert v.passed is False
    assert v.code is RejectCode.TRADE_LOSS


def test_open_requires_stop_loss(settings):
    d = _decision(leverage=2, size_pct=0.05, stop_loss_pct=0.0)
    v = validate(d, _ctx(), settings)
    assert v.passed is False
    assert v.code is RejectCode.TRADE_LOSS


def test_kill_switch_blocks_everything(settings):
    d = _decision(leverage=2)
    v = validate(d, _ctx(kill_switch=True), settings)
    assert v.passed is False
    assert v.code is RejectCode.KILL_SWITCH


def test_halt_new_entries(settings):
    v = validate(_decision(), _ctx(halt_new_entries=True), settings)
    assert v.code is RejectCode.HALT_NEW_ENTRIES
    assert v.reason == "new entries halted"


def test_halt_new_entries_uses_specific_reason(settings):
    v = validate(
        _decision(),
        _ctx(
            halt_new_entries=True,
            halt_new_entries_reason="engine stopping/restarting: signal",
        ),
        settings,
    )
    assert v.code is RejectCode.HALT_NEW_ENTRIES
    assert v.reason == "new entries halted: engine stopping/restarting: signal"


def test_daily_loss_breach(settings):
    v = validate(_decision(), _ctx(day_realized_pnl=-25.0), settings)
    assert v.code is RejectCode.DAILY_LOSS


def test_liq_distance_too_close(settings):
    """高杠杆使强平价过近 → 拒单。lev=3 时距离≈(1/3-0.005)*100≈32.8%，
    这里用 settings 改不动，构造一个 mmr 很大的 ctx 让距离<5%。"""
    # 1/3 - mmr < 0.05 → mmr > 0.2833
    d = _decision(leverage=3, size_pct=0.05)
    v = validate(d, _ctx(maintenance_margin_rate=0.30), settings)
    assert v.passed is False
    assert v.code is RejectCode.LIQ_DISTANCE


def test_liq_distance_helper_monotonic():
    near = estimate_liq_distance_pct(side=Action.OPEN_LONG, leverage=50, maintenance_margin_rate=0.005)
    far = estimate_liq_distance_pct(side=Action.OPEN_LONG, leverage=2, maintenance_margin_rate=0.005)
    assert far > near  # 杠杆越低，强平越远


def test_invalid_size(settings):
    v = validate(_decision(size_pct=0.0), _ctx(), settings)
    # size_pct=0 在 schema 合法(ge=0)，但风控判 INVALID_SIZE
    assert v.code is RejectCode.INVALID_SIZE
