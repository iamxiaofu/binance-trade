"""Normalize Binance USDT-M fills from private stream and ccxt REST."""
from __future__ import annotations

from typing import Any

from src.exchange.positions import normalize_symbol


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def private_order_trade_fill(event) -> dict[str, Any] | None:
    order = event.payload.get("o") or {}
    if str(order.get("x") or "").upper() != "TRADE":
        return None
    qty = _float(order.get("l"))
    price = _float(order.get("L"))
    trade_id = str(order.get("t") or "")
    if qty <= 0 or price <= 0 or not trade_id or trade_id == "-1":
        return None
    return {
        "ts_ms": int(order.get("T") or event.transaction_time_ms or event.event_time_ms or 0),
        "symbol": normalize_symbol(order.get("s")),
        "exchange_trade_id": trade_id,
        "exchange_order_id": str(order.get("i") or ""),
        "client_order_id": str(order.get("c") or ""),
        "side": str(order.get("S") or "").lower(),
        "qty": qty,
        "price": price,
        "fee": _float(order.get("n")),
        "fee_asset": str(order.get("N") or ""),
        "realized_pnl": _float(order.get("rp")),
        "liquidity": "maker" if bool(order.get("m")) else "taker",
        "reduce_only": bool(order.get("R")),
        "source": "stream",
        "raw": event.payload,
    }


def ccxt_trade_fill(trade: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    info = trade.get("info") or {}
    fee = trade.get("fee") or {}
    trade_id = str(trade.get("id") or info.get("id") or "")
    qty = _float(trade.get("amount") or info.get("qty"))
    price = _float(trade.get("price") or info.get("price"))
    if not trade_id or qty <= 0 or price <= 0:
        return None
    maker = trade.get("takerOrMaker") == "maker"
    if "maker" in info:
        maker = str(info.get("maker")).lower() == "true"
    return {
        "ts_ms": int(trade.get("timestamp") or info.get("time") or 0),
        "symbol": normalize_symbol(info.get("symbol") or symbol),
        "exchange_trade_id": trade_id,
        "exchange_order_id": str(trade.get("order") or info.get("orderId") or ""),
        "client_order_id": str(
            trade.get("clientOrderId")
            or info.get("clientOrderId")
            or info.get("clientOrderID")
            or ""
        ),
        "side": str(trade.get("side") or info.get("side") or "").lower(),
        "qty": qty,
        "price": price,
        "fee": _float(fee.get("cost") or info.get("commission")),
        "fee_asset": str(fee.get("currency") or info.get("commissionAsset") or ""),
        "realized_pnl": _float(trade.get("realizedPnl") or info.get("realizedPnl")),
        "liquidity": "maker" if maker else "taker",
        "reduce_only": str(info.get("reduceOnly") or "").lower() == "true",
        "source": "rest",
        "raw": trade,
    }
