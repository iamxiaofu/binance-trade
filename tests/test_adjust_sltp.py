"""ADJUST_SLTP action 单测：schema 解析、触发价换算、校验逻辑。"""
from __future__ import annotations

import pytest

from src.llm.schema import Action, PositionSnapshot, TradeDecision


# ── helpers ──────────────────────────────────────────────────────────────────

def _decision(**kwargs) -> TradeDecision:
    defaults = dict(
        symbol="BTCUSDT", action="ADJUST_SLTP", confidence=0.6,
        size_pct=0.0, leverage=1, stop_loss_pct=0.01, take_profit_pct=0.02,
        reason="adjust test",
    )
    return TradeDecision(**{**defaults, **kwargs})


# ── schema tests ──────────────────────────────────────────────────────────────

def test_adjust_sltp_action_exists():
    assert Action.ADJUST_SLTP == "ADJUST_SLTP"


def test_adjust_sltp_parses():
    d = _decision()
    assert d.action is Action.ADJUST_SLTP
    assert d.is_adjust is True
    assert d.is_open is False


def test_adjust_sltp_stop_loss_pct_bounds():
    with pytest.raises(Exception):
        _decision(stop_loss_pct=1.5)   # > 1.0 → validation error
    with pytest.raises(Exception):
        _decision(stop_loss_pct=-0.01)  # < 0 → validation error


def test_position_snapshot_sl_tp_fields():
    pos = PositionSnapshot(
        has_position=True, side="LONG", entry_price=60000.0,
        sl_price=59000.0, tp_price=63000.0,
    )
    assert pos.sl_price == 59000.0
    assert pos.tp_price == 63000.0


def test_position_snapshot_sl_tp_default_none():
    pos = PositionSnapshot()
    assert pos.sl_price is None
    assert pos.tp_price is None


# ── trigger price calculation (mark-based) ────────────────────────────────────

def _calc_trigger(mark: float, pct: float, kind: str, side: str) -> float:
    """复现 _handle_adjust_sltp 里的换算公式。"""
    is_long = side == "long"
    if kind == "SL":
        return mark * (1 - pct) if is_long else mark * (1 + pct)
    else:  # TP
        return mark * (1 + pct) if is_long else mark * (1 - pct)


def test_long_sl_below_mark():
    """多单止损必须低于标记价。"""
    mark = 65000.0
    sl = _calc_trigger(mark, 0.02, "SL", "long")
    assert sl < mark
    assert abs(sl - 63700.0) < 1.0  # 65000 * 0.98 = 63700


def test_long_tp_above_mark():
    """多单止盈必须高于标记价。"""
    mark = 65000.0
    tp = _calc_trigger(mark, 0.03, "TP", "long")
    assert tp > mark
    assert abs(tp - 66950.0) < 1.0  # 65000 * 1.03 = 66950


def test_short_sl_above_mark():
    """空单止损必须高于标记价。"""
    mark = 65000.0
    sl = _calc_trigger(mark, 0.02, "SL", "short")
    assert sl > mark


def test_short_tp_below_mark():
    """空单止盈必须低于标记价。"""
    mark = 65000.0
    tp = _calc_trigger(mark, 0.03, "TP", "short")
    assert tp < mark


def test_profit_lock_long():
    """多单行情上涨后，止损移至开仓价之上（锁利），合法（mark>entry, SL>entry 且 SL<mark）。"""
    entry = 60000.0
    mark = 65000.0
    sl_pct = 0.01        # SL = 65000 * 0.99 = 64350 > entry=60000 ✓
    sl = _calc_trigger(mark, sl_pct, "SL", "long")
    assert sl > entry    # 止损在盈利侧
    assert sl < mark     # 止损低于标记价


# ── _validate_adjust_trigger equivalent logic ─────────────────────────────────

class _MockSettings:
    class risk:
        max_loss_per_trade_pct = 2.0  # 2% of equity


class _MockEngine:
    """模拟引擎 _validate_adjust_trigger 的纯逻辑版本（不依赖 loop.py 实例）。"""
    _settings = _MockSettings()

    def validate(self, *, side, kind, trigger, entry, mark, qty, equity) -> str:
        if trigger <= 0 or mark <= 0 or qty <= 0:
            return "价格或数量无效"
        if side == "long":
            if kind == "SL" and trigger >= mark:
                return f"多单止损必须低于 mark"
            if kind == "TP" and trigger <= mark:
                return f"多单止盈必须高于 mark"
        elif side == "short":
            if kind == "SL" and trigger <= mark:
                return f"空单止损必须高于 mark"
            if kind == "TP" and trigger >= mark:
                return f"空单止盈必须低于 mark"
        else:
            return "未知持仓方向"

        if kind == "SL" and equity > 0:
            loss = (entry - trigger) * qty if side == "long" else (trigger - entry) * qty
            if loss >= 0:
                max_loss = equity * (self._settings.risk.max_loss_per_trade_pct / 100.0)
                if max_loss > 0 and loss > max_loss:
                    return f"止损亏损 {loss:.2f} 超过上限 {max_loss:.2f}"
        return ""


_eng = _MockEngine()


def test_validate_long_sl_valid():
    assert _eng.validate(side="long", kind="SL", trigger=64000, entry=62000,
                         mark=65000, qty=0.01, equity=5000) == ""


def test_validate_long_sl_above_mark_rejected():
    err = _eng.validate(side="long", kind="SL", trigger=66000, entry=62000,
                        mark=65000, qty=0.01, equity=5000)
    assert err != ""


def test_validate_profit_lock_passes():
    """止损在开仓价之上（锁利）时，loss<0，不触发 max_loss 限制。"""
    assert _eng.validate(side="long", kind="SL", trigger=63000, entry=62000,
                         mark=65000, qty=0.01, equity=5000) == ""


def test_validate_sl_exceeds_max_loss_rejected():
    """止损距离过大超过 max_loss_per_trade_pct，应被拒绝。"""
    # equity=1000, max_loss=2% = 20 USDT; qty=1 BTC, entry=mark=65000
    # trigger=64900 → loss=(65000-64900)*1=100 > 20
    err = _eng.validate(side="long", kind="SL", trigger=64900, entry=65000,
                        mark=65000, qty=1.0, equity=1000)
    assert err != ""


def test_validate_short_tp_valid():
    assert _eng.validate(side="short", kind="TP", trigger=63000, entry=65000,
                         mark=64000, qty=0.01, equity=5000) == ""


def test_validate_short_tp_above_mark_rejected():
    err = _eng.validate(side="short", kind="TP", trigger=65000, entry=66000,
                        mark=64000, qty=0.01, equity=5000)
    assert err != ""
