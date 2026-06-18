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
    # Binance ALGO_UPDATE uses ``tp`` for the condition trigger price, while
    # REST/ccxt responses normally expose ``triggerPrice`` or ``stopPrice``.
    trigger = _float(_value(o, info, "triggerPrice", "stopPrice", "tp"))
    price = _float(_value(o, info, "price"))
    filled_qty = abs(_float(_value(o, info, "filled", "filledQty", "executedQty", "actualQty")))
    filled_price = _float(_value(o, info, "average", "avgPrice", "actualPrice"))
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
        "filled_qty": filled_qty,
        "filled_price": filled_price,
        "avg_price": filled_price,
        "trigger_price": trigger,
        "status": _status(o, info),
        "raw_status": str(_value(o, info, "status", "algoStatus") or ""),
        "reduce_only": _bool(_value(o, info, "reduceOnly")),
        "client_algo_id": str(_value(o, info, "clientAlgoId") or ""),
        "position_side": str(_value(o, info, "positionSide") or ""),
        "ts_ms": ts_ms,
    }


def normalize_open_order(order: Mapping[str, Any] | None) -> dict[str, Any]:
    """归一化 ccxt 拉到的「普通未成交挂单」（限价/限价 maker/reduce-only 等）。

    不包含 SL/TP 算法单（走 ``normalize_condition_order``）。
    """
    o = order or {}
    info = o.get("info") if isinstance(o.get("info"), Mapping) else {}
    order_type = str(_value(o, info, "type", "orderType") or "").upper()
    qty = abs(_float(_value(o, info, "amount", "quantity", "origQty")))
    price = _float(_value(o, info, "price"))
    filled_qty = abs(_float(_value(o, info, "filled", "executedQty")))
    avg_price = _float(_value(o, info, "average", "avgPrice"))
    ts_ms = int(_float(_value(o, info, "updateTime", "timestamp", "time", "createTime")))

    return {
        "id": str(_value(o, info, "id", "orderId") or ""),
        "symbol": normalize_symbol(_value(o, info, "symbol")),
        "side": str(_value(o, info, "side") or "").lower(),
        "order_type": order_type or "LIMIT",
        "qty": qty,
        "price": price,
        "filled_qty": filled_qty,
        "avg_price": avg_price,
        "status": _status(o, info),
        "raw_status": str(_value(o, info, "status") or ""),
        "time_in_force": str(_value(o, info, "timeInForce") or "").upper(),
        "reduce_only": _bool(_value(o, info, "reduceOnly")),
        "client_order_id": str(_value(o, info, "clientOrderId", "origClientOrderId") or ""),
        "position_side": str(_value(o, info, "positionSide") or ""),
        "ts_ms": ts_ms,
    }
