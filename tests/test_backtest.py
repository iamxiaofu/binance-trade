"""backtest/replay.py 测试：节流计数与风控夹断的确定性行为。"""
from __future__ import annotations

from src.backtest.replay import BacktestStats, fixed_provider, replay


def _flat_klines(n: int, price: float = 100.0) -> list[list[float]]:
    """完全不动的价格序列 → throttle 仅靠 max_skip_cycles 兜底触发。"""
    return [[i * 60_000, price, price, price, price, 10.0] for i in range(n)]


def _trending_klines(n: int, start: float = 100.0, step: float = 1.0) -> list[list[float]]:
    out = []
    for i in range(n):
        p = start + i * step
        out.append([i * 60_000, p, p + 0.5, p - 0.5, p, 10.0])
    return out


def test_flat_market_triggers_only_on_skip_cap(settings):
    # price_change_pct=0.3 永不达到（价格不动），靠 max_skip_cycles=6 兜底
    settings.throttle.max_skip_cycles = 6
    kl = _flat_klines(120)
    provider = fixed_provider(dict(action="HOLD", confidence=0.5, size_pct=0.0,
                                   leverage=1, stop_loss_pct=0.0, take_profit_pct=0.0,
                                   reason="hold"))
    stats = replay(symbol="BTCUSDT", klines=kl, settings=settings, provider=provider, window=50)
    assert isinstance(stats, BacktestStats)
    assert stats.cycles == len(kl) - 50
    # 触发次数应远小于周期数（大多被跳过）
    assert stats.triggered < stats.cycles
    assert stats.triggered >= 1
    assert stats.skipped + stats.triggered == stats.cycles


def test_leverage_clamp_is_counted(settings):
    settings.risk.max_leverage = 3
    settings.throttle.price_change_pct = 0.0  # 每周期都触发
    kl = _trending_klines(100)
    # 杠杆 10 > max_leverage 3 → 必被 LEVERAGE_EXCEEDED 拒
    provider = fixed_provider(dict(action="OPEN_LONG", confidence=0.9, size_pct=0.1,
                                   leverage=10, stop_loss_pct=0.02, take_profit_pct=0.04,
                                   reason="too much leverage"))
    stats = replay(symbol="BTCUSDT", klines=kl, settings=settings, provider=provider, window=50)
    assert stats.triggered > 0
    assert stats.passed == 0
    assert stats.reject_codes.get("LEVERAGE_EXCEEDED", 0) == stats.triggered


def test_valid_decision_passes_risk(settings):
    settings.risk.max_leverage = 5
    settings.risk.min_confidence = 0.6
    settings.risk.max_order_margin_pct = 0.5  # equity_base=200 → 保证金上限 100
    settings.throttle.price_change_pct = 0.0
    kl = _trending_klines(80)
    provider = fixed_provider(dict(action="OPEN_LONG", confidence=0.9, size_pct=0.05,
                                   leverage=2, stop_loss_pct=0.02, take_profit_pct=0.04,
                                   reason="ok"))
    stats = replay(symbol="BTCUSDT", klines=kl, settings=settings,
                   provider=provider, window=50, available_margin=200.0)
    assert stats.triggered > 0
    assert stats.passed == stats.triggered
    assert not stats.reject_codes


def test_summary_shape(settings):
    kl = _flat_klines(70)
    provider = fixed_provider(dict(action="HOLD", confidence=0.5, size_pct=0.0,
                                   leverage=1, stop_loss_pct=0.0, take_profit_pct=0.0,
                                   reason="h"))
    stats = replay(symbol="BTCUSDT", klines=kl, settings=settings, provider=provider, window=50)
    s = stats.summary()
    assert set(s) == {"cycles", "triggered", "skipped", "passed_risk", "rejects"}
