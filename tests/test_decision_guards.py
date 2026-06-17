from src.engine.decision_guards import (
    CloseConfirmationState,
    SltpAdjustState,
    evaluate_close_guard,
    evaluate_sltp_adjust_guard,
)


def _close_guard(**overrides):
    params = {
        "state": None,
        "trade_id": 1,
        "opened_at_ms": 1,
        "now_ms": 10 * 60 * 1000,
        "side": "long",
        "entry_price": 100.0,
        "mark_price": 98.5,
        "atr": 1.0,
        "min_age_seconds": 300,
        "min_confirmations": 3,
        "loss_atr_multiple": 1.0,
        "confirmation_window_seconds": 900,
    }
    params.update(overrides)
    return evaluate_close_guard(**params)


def test_close_guard_blocks_before_minimum_age():
    result = _close_guard(opened_at_ms=8 * 60 * 1000, now_ms=10 * 60 * 1000)

    assert not result.allowed
    assert "position age" in result.reason
    assert result.state.count == 1


def test_close_guard_blocks_when_loss_is_less_than_atr():
    state = CloseConfirmationState(trade_id=1, count=2, last_close_ts_ms=9 * 60 * 1000)

    result = _close_guard(
        state=state,
        opened_at_ms=1,
        now_ms=10 * 60 * 1000,
        mark_price=99.5,
    )

    assert not result.allowed
    assert "floating loss" in result.reason
    assert result.state.count == 3


def test_close_guard_requires_three_consecutive_confirmations():
    state = None
    for i in range(2):
        result = _close_guard(
            state=state,
            opened_at_ms=1,
            now_ms=(10 + i) * 60 * 1000,
            mark_price=98.0,
        )
        assert not result.allowed
        assert "close confirmations" in result.reason
        state = result.state

    result = _close_guard(
        state=state,
        opened_at_ms=1,
        now_ms=12 * 60 * 1000,
        mark_price=98.0,
    )

    assert result.allowed
    assert result.state.count == 3


def test_sltp_guard_blocks_frequent_same_trade_adjustment():
    result = evaluate_sltp_adjust_guard(
        state=SltpAdjustState(trade_id=1, ts_ms=100_000, sl_price=96.0),
        trade_id=1,
        now_ms=200_000,
        side="long",
        entry_price=100.0,
        mark_price=104.0,
        old_sl=96.0,
        new_sl=97.0,
        atr=1.0,
        min_interval_seconds=900,
        min_improve_atr_multiple=0.4,
        breakeven_buffer_pct=0.15,
    )

    assert not result.allowed
    assert "interval" in result.reason


def test_sltp_guard_blocks_tiny_sl_improvement():
    result = evaluate_sltp_adjust_guard(
        state=SltpAdjustState(trade_id=1, ts_ms=0, sl_price=96.0),
        trade_id=1,
        now_ms=901_000,
        side="long",
        entry_price=100.0,
        mark_price=104.0,
        old_sl=96.0,
        new_sl=96.2,
        atr=1.0,
        min_interval_seconds=900,
        min_improve_atr_multiple=0.4,
        breakeven_buffer_pct=0.15,
    )

    assert not result.allowed
    assert "SL improvement" in result.reason


def test_sltp_guard_blocks_breakeven_without_fee_buffer():
    result = evaluate_sltp_adjust_guard(
        state=SltpAdjustState(trade_id=1, ts_ms=0, sl_price=99.0),
        trade_id=1,
        now_ms=901_000,
        side="long",
        entry_price=100.0,
        mark_price=104.0,
        old_sl=99.0,
        new_sl=100.05,
        atr=1.0,
        min_interval_seconds=900,
        min_improve_atr_multiple=0.4,
        breakeven_buffer_pct=0.15,
    )

    assert not result.allowed
    assert "fee/slippage" in result.reason


def test_sltp_guard_allows_meaningful_profit_lock():
    result = evaluate_sltp_adjust_guard(
        state=SltpAdjustState(trade_id=1, ts_ms=0, sl_price=99.0),
        trade_id=1,
        now_ms=901_000,
        side="long",
        entry_price=100.0,
        mark_price=104.0,
        old_sl=99.0,
        new_sl=100.5,
        atr=1.0,
        min_interval_seconds=900,
        min_improve_atr_multiple=0.4,
        breakeven_buffer_pct=0.15,
    )

    assert result.allowed
