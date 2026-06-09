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


# Binance USDT-M ISOLATED 模式返回的 position 字典里 leverage 字段缺失，
# 仅靠 notional / initial_margin / isolated_wallet 可以反推。下表是常见档位。
_LEVERAGE_LADDER = [1, 2, 3, 5, 10, 20, 25, 50, 75, 100, 125]


def _derive_leverage_from_margin(
    notional: float,
    initial_margin: float,
    isolated_margin: float,
) -> int:
    """从 notional + 保证金反推杠杆（ISOLATED 模式交易所不返回 leverage 字段时用）。"""
    base = initial_margin if initial_margin > 0 else isolated_margin
    if notional <= 0 or base <= 0:
        return 0
    raw = notional / base
    if raw <= 1.0:
        return 1
    # 取最接近且不小于 raw 的档位
    for lev in _LEVERAGE_LADDER:
        if lev >= raw - 0.5:
            return lev
    return _LEVERAGE_LADDER[-1]


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

    # B6：leverage 字段在 ISOLATED 模式下常常为 null。从 notional/initial_margin 反推。
    raw_lev = _float(_value(p, info, "leverage"))
    if raw_lev > 0:
        leverage = int(raw_lev)
    else:
        leverage = _derive_leverage_from_margin(notional, initial_margin, isolated_margin)

    return {
        "symbol": sym,
        "side": side,
        "contracts": contracts,
        "entry_price": entry,
        "mark_price": mark,
        "leverage": leverage,
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
