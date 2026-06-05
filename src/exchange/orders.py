"""Order normalization helpers for exchange-synced protective orders."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.exchange.positions import normalize_symbol


def _value(order: Mapping[str, Any], info: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        val = order.get(key)
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


def _bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "y")
    return bool(val)


def _status(order: Mapping[str, Any], info: Mapping[str, Any]) -> str:
    raw = str(_value(order, info, "status", "algoStatus") or "").lower()
    return {
        "new": "placed",
        "open": "placed",
        "working": "placed",
        "canceled": "canceled",
        "cancelled": "canceled",
        "expired": "expired",
        "closed": "filled",
        "filled": "filled",
        "triggered": "triggered",
        "finished": "filled",
    }.get(raw, raw or "unknown")


def _kind(order_type: str) -> str:
    upper = order_type.upper()
    if upper.startswith("STOP"):
        return "SL"
    if upper.startswith("TAKE_PROFIT"):
        return "TP"
    return ""


def normalize_condition_order(order: Mapping[str, Any] | None) -> dict[str, Any]:
    o = order or {}
    info = o.get("info") if isinstance(o.get("info"), Mapping) else {}
    order_type = str(_value(o, info, "orderType", "type") or "").upper()
    qty = abs(_float(_value(o, info, "amount", "quantity", "origQty")))
    trigger = _float(_value(o, info, "triggerPrice", "stopPrice"))
    price = _float(_value(o, info, "price"))
    ts_ms = int(_float(_value(o, info, "updateTime", "timestamp", "createTime")))
    if not ts_ms:
        ts_ms = int(_float(_value(o, info, "timestamp", "createTime")))

    return {
        "id": str(_value(o, info, "id", "algoId", "orderId") or ""),
        "symbol": normalize_symbol(_value(o, info, "symbol")),
        "kind": _kind(order_type),
        "side": str(_value(o, info, "side") or "").lower(),
        "order_type": order_type,
        "qty": qty,
        "price": price,
        "trigger_price": trigger,
        "status": _status(o, info),
        "raw_status": str(_value(o, info, "status", "algoStatus") or ""),
        "reduce_only": _bool(_value(o, info, "reduceOnly")),
        "client_algo_id": str(_value(o, info, "clientAlgoId") or ""),
        "position_side": str(_value(o, info, "positionSide") or ""),
        "ts_ms": ts_ms,
    }
