"""Hard execution guards for LLM close and SL/TP adjustment decisions."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CloseConfirmationState:
    trade_id: int = 0
    first_close_ts_ms: int = 0
    last_close_ts_ms: int = 0
    count: int = 0


@dataclass(frozen=True)
class CloseGuardResult:
    allowed: bool
    state: CloseConfirmationState
    reason: str = ""


@dataclass
class SltpAdjustState:
    trade_id: int = 0
    ts_ms: int = 0
    sl_price: float = 0.0


@dataclass(frozen=True)
class SltpGuardResult:
    allowed: bool
    reason: str = ""


def _loss_abs(entry: float, mark: float, side: str) -> float:
    if side == "long":
        return max(entry - mark, 0.0)
    if side == "short":
        return max(mark - entry, 0.0)
    return 0.0


def evaluate_close_guard(
    *,
    state: CloseConfirmationState | None,
    trade_id: int,
    opened_at_ms: int,
    now_ms: int,
    side: str,
    entry_price: float,
    mark_price: float,
    atr: float,
    min_age_seconds: int,
    min_confirmations: int,
    loss_atr_multiple: float,
    confirmation_window_seconds: int,
) -> CloseGuardResult:
    """Return whether an LLM CLOSE can be executed.

    The guard intentionally blocks early/microstructure-driven exits. It does not
    replace exchange SL/TP protection or emergency flattening paths.
    """
    if trade_id <= 0 or opened_at_ms <= 0:
        return CloseGuardResult(True, state or CloseConfirmationState(), "missing trade lifecycle")

    if state is None or state.trade_id != trade_id:
        state = CloseConfirmationState(trade_id=trade_id)

    if (
        state.last_close_ts_ms > 0
        and confirmation_window_seconds > 0
        and now_ms - state.last_close_ts_ms > confirmation_window_seconds * 1000
    ):
        state = CloseConfirmationState(trade_id=trade_id)

    if state.count == 0:
        state.first_close_ts_ms = now_ms
    state.last_close_ts_ms = now_ms
    state.count += 1

    age_seconds = max(0, (now_ms - opened_at_ms) / 1000.0)
    if age_seconds < min_age_seconds:
        return CloseGuardResult(
            False,
            state,
            f"position age {age_seconds:.0f}s < close_confirm_min_age {min_age_seconds}s",
        )

    loss = _loss_abs(entry_price, mark_price, side)
    atr_threshold = max(float(atr or 0.0), 0.0) * max(float(loss_atr_multiple), 0.0)
    if atr_threshold > 0 and loss < atr_threshold:
        return CloseGuardResult(
            False,
            state,
            f"floating loss {loss:.8g} < {loss_atr_multiple:g} ATR ({atr_threshold:.8g})",
        )

    if state.count < min_confirmations:
        return CloseGuardResult(
            False,
            state,
            f"close confirmations {state.count} < required {min_confirmations}",
        )

    return CloseGuardResult(True, state, "confirmed close")


def evaluate_sltp_adjust_guard(
    *,
    state: SltpAdjustState | None,
    trade_id: int,
    now_ms: int,
    side: str,
    entry_price: float,
    mark_price: float,
    old_sl: float,
    new_sl: float,
    atr: float,
    min_interval_seconds: int,
    min_improve_atr_multiple: float,
    breakeven_buffer_pct: float,
) -> SltpGuardResult:
    """Validate whether a new SL is meaningful enough to replace the old one."""
    if new_sl <= 0:
        return SltpGuardResult(True, "no SL change")

    if trade_id > 0 and state is not None and state.trade_id == trade_id:
        elapsed = max(0, now_ms - state.ts_ms)
        if elapsed < min_interval_seconds * 1000:
            return SltpGuardResult(
                False,
                f"SLTP adjust interval {elapsed / 1000:.0f}s < {min_interval_seconds}s",
            )

    side = (side or "").lower()
    if old_sl > 0:
        if side == "long":
            improvement = new_sl - old_sl
        elif side == "short":
            improvement = old_sl - new_sl
        else:
            improvement = 0.0
        min_improvement = max(float(atr or 0.0), 0.0) * max(float(min_improve_atr_multiple), 0.0)
        if min_improvement > 0 and improvement < min_improvement:
            return SltpGuardResult(
                False,
                f"SL improvement {improvement:.8g} < {min_improve_atr_multiple:g} ATR ({min_improvement:.8g})",
            )

    # If SL is moved to or beyond entry, it is a breakeven/profit-lock SL and
    # must cover expected round-trip fees plus slippage.
    buffer = max(float(breakeven_buffer_pct or 0.0), 0.0) / 100.0
    if buffer > 0 and entry_price > 0:
        if side == "long" and new_sl >= entry_price:
            min_sl = entry_price * (1 + buffer)
            if new_sl < min_sl:
                return SltpGuardResult(
                    False,
                    f"long breakeven SL {new_sl:.8g} < fee/slippage floor {min_sl:.8g}",
                )
        if side == "short" and new_sl <= entry_price:
            max_sl = entry_price * (1 - buffer)
            if new_sl > max_sl:
                return SltpGuardResult(
                    False,
                    f"short breakeven SL {new_sl:.8g} > fee/slippage floor {max_sl:.8g}",
                )

    return SltpGuardResult(True, "valid SL adjustment")
