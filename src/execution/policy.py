"""Execution price policy helpers.

LLM 只给方向和风险参数；真实挂单价格由执行策略按盘口计算。
"""
from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from src.config.schema import ExecutionConfig
from src.exchange.filters import SymbolFilters, round_price


@dataclass(frozen=True)
class MakerQuote:
    price: float
    source: str
    best_bid: float = 0.0
    best_ask: float = 0.0


class ExecutionPolicy:
    def __init__(self, cfg: ExecutionConfig):
        self._cfg = cfg

    async def maker_quote(
        self,
        *,
        client,
        symbol: str,
        side: str,
        fallback_price: float,
        filters: SymbolFilters,
    ) -> MakerQuote:
        """Return a post-only limit price that should not cross the book."""
        best_bid = 0.0
        best_ask = 0.0
        source = "fallback"
        try:
            book = await client.fetch_order_book(symbol, limit=5)
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if bids:
                best_bid = float(bids[0][0] or 0.0)
            if asks:
                best_ask = float(asks[0][0] or 0.0)
            if best_bid > 0 or best_ask > 0:
                source = "orderbook"
        except Exception as e:
            logger.warning("[{}] maker quote orderbook fallback: {}", symbol, e)

        offset = self._cfg.maker_price_offset_bps / 10000.0
        if side == "buy":
            base = best_bid if best_bid > 0 else fallback_price
            raw = base * (1.0 - offset)
        else:
            base = best_ask if best_ask > 0 else fallback_price
            raw = base * (1.0 + offset)

        return MakerQuote(
            price=float(round_price(raw, filters)),
            source=source,
            best_bid=best_bid,
            best_ask=best_ask,
        )
