"""Feature snapshots for deciding whether the LLM should be called.

The snapshot is intentionally smaller than the full LLM context. It captures
state transitions that are meaningful enough to re-evaluate a symbol, without
forcing the engine to call the LLM every cycle.
"""
from __future__ import annotations

import time

from pydantic import BaseModel, Field

from src.exchange.market_data import SymbolSnapshot
from src.features.indicators import compute_snapshot, compute_timeframe_brief
from src.llm.schema import PositionSnapshot


class FeatureSnapshot(BaseModel):
    symbol: str
    ts_ms: int
    last_price: float
    mark_price: float

    trend_direction: str = "flat"
    trend_score: float = 0.0
    ema_spread_pct: float = 0.0
    macd_hist: float = 0.0
    rsi: float = 50.0
    atr_pct: float = 0.0
    boll_bandwidth_pct: float = 0.0
    volume_ratio: float = 1.0
    volume_zscore_20: float = 0.0

    micro_return_5_pct: float = 0.0
    micro_return_15_pct: float = 0.0
    micro_range_5_pct: float = 0.0
    micro_volume_zscore: float = 0.0

    higher_trends: dict[str, str] = Field(default_factory=dict)
    higher_rsi: dict[str, float] = Field(default_factory=dict)
    higher_macd: dict[str, float] = Field(default_factory=dict)

    leader_symbol: str | None = None
    leader_trend_direction: str = ""
    leader_ema_spread_pct: float = 0.0
    leader_micro_return_5_pct: float = 0.0
    leader_volume_zscore_20: float = 0.0

    has_position: bool = False
    position_side: str | None = None
    unrealized_pnl_pct: float | None = None
    current_leverage: int | None = None


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct_change(values: list[float], periods: int) -> float:
    if len(values) <= periods:
        return 0.0
    base = values[-periods - 1]
    if base == 0:
        return 0.0
    return (values[-1] - base) / base * 100.0


def _micro_stats(klines: list[list[float]]) -> dict[str, float]:
    if not klines:
        return {
            "micro_return_5_pct": 0.0,
            "micro_return_15_pct": 0.0,
            "micro_range_5_pct": 0.0,
            "micro_volume_zscore": 0.0,
        }
    closes = [_safe_float(k[4]) for k in klines]
    highs = [_safe_float(k[2]) for k in klines]
    lows = [_safe_float(k[3]) for k in klines]
    vols = [_safe_float(k[5]) for k in klines]
    last_close = closes[-1] if closes else 0.0
    recent_high = max(highs[-5:]) if highs else 0.0
    recent_low = min(lows[-5:]) if lows else 0.0
    micro_range_5 = ((recent_high - recent_low) / last_close * 100.0) if last_close > 0 else 0.0

    vol_z = 0.0
    if len(vols) >= 10:
        mean = sum(vols) / len(vols)
        variance = sum((v - mean) ** 2 for v in vols) / len(vols)
        std = variance ** 0.5
        if std > 0:
            vol_z = (vols[-1] - mean) / std

    return {
        "micro_return_5_pct": round(_pct_change(closes, 5), 4),
        "micro_return_15_pct": round(_pct_change(closes, 15), 4),
        "micro_range_5_pct": round(micro_range_5, 4),
        "micro_volume_zscore": round(vol_z, 4),
    }


def build_feature_snapshot(
    *,
    symbol: str,
    snapshot: SymbolSnapshot,
    position: PositionSnapshot,
    higher_tf_klines: dict[str, list[list[float]]] | None = None,
    micro_klines: list[list[float]] | None = None,
    leader_snapshot: FeatureSnapshot | None = None,
) -> FeatureSnapshot | None:
    if not snapshot.is_ready or len(snapshot.klines) < 30:
        return None

    ind = compute_snapshot(snapshot.klines)
    higher_trends: dict[str, str] = {}
    higher_rsi: dict[str, float] = {}
    higher_macd: dict[str, float] = {}
    for tf, klines in (higher_tf_klines or {}).items():
        if klines and len(klines) >= 30:
            brief = compute_timeframe_brief(klines, tf)
            higher_trends[tf] = str(brief.get("trend") or "flat")
            higher_rsi[tf] = _safe_float(brief.get("rsi"), 50.0)
            higher_macd[tf] = _safe_float(brief.get("macd"))

    micro = _micro_stats(micro_klines or [])

    return FeatureSnapshot(
        symbol=symbol,
        ts_ms=snapshot.updated_ms or int(time.time() * 1000),
        last_price=snapshot.last_price,
        mark_price=snapshot.mark_price or snapshot.last_price,
        trend_direction=str(ind.get("trend_direction") or "flat"),
        trend_score=_safe_float(ind.get("trend_score")),
        ema_spread_pct=_safe_float(ind.get("ema_spread_pct")),
        macd_hist=_safe_float(ind.get("macd_hist")),
        rsi=_safe_float(ind.get("rsi"), 50.0),
        atr_pct=_safe_float(ind.get("atr_pct")),
        boll_bandwidth_pct=_safe_float(ind.get("boll_bandwidth_pct")),
        volume_ratio=_safe_float(ind.get("volume_ratio"), 1.0),
        volume_zscore_20=_safe_float(ind.get("volume_zscore_20")),
        higher_trends=higher_trends,
        higher_rsi=higher_rsi,
        higher_macd=higher_macd,
        leader_symbol=leader_snapshot.symbol if leader_snapshot is not None else None,
        leader_trend_direction=(
            leader_snapshot.trend_direction if leader_snapshot is not None else ""
        ),
        leader_ema_spread_pct=(
            leader_snapshot.ema_spread_pct if leader_snapshot is not None else 0.0
        ),
        leader_micro_return_5_pct=(
            leader_snapshot.micro_return_5_pct if leader_snapshot is not None else 0.0
        ),
        leader_volume_zscore_20=(
            leader_snapshot.volume_zscore_20 if leader_snapshot is not None else 0.0
        ),
        has_position=position.has_position,
        position_side=position.side,
        unrealized_pnl_pct=position.unrealized_pnl_pct,
        current_leverage=position.current_leverage,
        **micro,
    )
