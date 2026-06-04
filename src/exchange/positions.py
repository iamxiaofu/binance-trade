"""Position normalization helpers shared by the engine store and web dashboard."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def normalize_symbol(symbol: Any) -> str:
    raw = str(symbol or "").upper()
    return raw.replace("/USDT:USDT", "USDT").replace(":USDT", "").replace("/", "")


def _value(position: Mapping[str, Any], info: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        val = position.get(key)
        if val not in (None, ""):
            return val
        val = info.get(key)
        if val not in (None, ""):
            return val
    return None


def _float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def normalize_position(
    position: Mapping[str, Any] | None,
    *,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Convert a ccxt/Binance position dict into dashboard/store fields."""
    p = position or {}
    info = p.get("info") if isinstance(p.get("info"), Mapping) else {}
    sym = normalize_symbol(symbol or _value(p, info, "symbol"))

    signed_amount = _float(_value(p, info, "positionAmt", "contracts", "amount"))
    contracts = abs(signed_amount)

    side = str(_value(p, info, "side", "positionSide") or "").lower()
    if side not in ("long", "short"):
        if signed_amount < 0:
            side = "short"
        elif signed_amount > 0:
            side = "long"
        else:
            side = ""

    entry = _float(_value(p, info, "entryPrice", "entry_price"))
    mark = _float(_value(p, info, "markPrice", "mark_price", "mark"), entry)
    notional = abs(_float(_value(p, info, "notional"), contracts * mark))
    initial_margin = _float(
        _value(p, info, "initialMargin", "positionInitialMargin", "isolatedMargin")
    )
    maintenance_margin = _float(_value(p, info, "maintenanceMargin", "maintMargin"))
    isolated_margin = _float(_value(p, info, "collateral", "isolatedMargin"))
    unrealized_pnl = _float(
        _value(p, info, "unrealizedPnl", "unRealizedProfit", "unrealized_pnl")
    )
    roi_pct = _float(_value(p, info, "percentage"), None)
    if roi_pct is None:
        roi_pct = (unrealized_pnl / initial_margin * 100.0) if initial_margin else 0.0

    return {
        "symbol": sym,
        "side": side,
        "contracts": contracts,
        "entry_price": entry,
        "mark_price": mark,
        "leverage": int(_float(_value(p, info, "leverage"))),
        "unrealized_pnl": unrealized_pnl,
        "notional": notional,
        "initial_margin": initial_margin,
        "isolated_margin": isolated_margin,
        "maintenance_margin": maintenance_margin,
        "roi_pct": roi_pct,
        "liquidation_price": _float(_value(p, info, "liquidationPrice")),
        "margin_ratio": _float(_value(p, info, "marginRatio")),
        "margin_mode": str(_value(p, info, "marginMode", "marginType") or ""),
        "ts_ms": int(_float(_value(p, info, "timestamp", "updateTime"))),
    }
