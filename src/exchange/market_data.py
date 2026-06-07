"""行情数据：为每个 symbol 维护最新价/标记价/资金费率/K线快照。

设计取舍：主循环是 5 分钟级别，对延迟不敏感，因此默认用 **REST 轮询**
（稳定、实现简单、ccxt.async_support 原生支持），并预留 ``start``/``stop``
生命周期方法，后续若接 ccxt.pro 的 ``watch_*`` 可在不改调用方的前提下替换。

REST 兜底：每次 refresh 失败不抛出致命错误，保留上一次快照并记日志，
由上层在快照过期时决定降级（features 会校验 staleness）。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from loguru import logger

from src.config.schema import Settings
from src.exchange.client import ExchangeClient


@dataclass
class SymbolSnapshot:
    symbol: str
    last_price: float = 0.0
    mark_price: float = 0.0
    funding_rate: float = 0.0
    change_24h_pct: float = 0.0
    klines: list[list[float]] = field(default_factory=list)  # [[ts,o,h,l,c,v],...]
    updated_ms: int = 0

    @property
    def is_ready(self) -> bool:
        return self.last_price > 0 and len(self.klines) > 0

    def age_ms(self) -> int:
        return int(time.time() * 1000) - self.updated_ms if self.updated_ms else 1 << 62


class MarketData:
    """聚合所有 symbol 的行情快照。"""

    def __init__(self, client: ExchangeClient, settings: Settings):
        self._client = client
        self._settings = settings
        self._snapshots: dict[str, SymbolSnapshot] = {
            s: SymbolSnapshot(symbol=s) for s in settings.symbols
        }
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """首次全量拉取，确保启动即有可用快照。"""
        await self.refresh_all()

    async def stop(self) -> None:
        # REST 模式无长连接需要关闭；保留接口以兼容未来 websocket
        return None

    def snapshot(self, symbol: str) -> SymbolSnapshot:
        self.ensure_symbol(symbol)
        return self._snapshots[symbol]

    def ensure_symbol(self, symbol: str) -> SymbolSnapshot:
        symbol = symbol.upper().strip()
        if symbol not in self._snapshots:
            self._snapshots[symbol] = SymbolSnapshot(symbol=symbol)
        return self._snapshots[symbol]

    async def refresh_all(self, symbols: list[str] | None = None) -> None:
        target = symbols or self._settings.symbols
        for symbol in target:
            self.ensure_symbol(symbol)
        await asyncio.gather(*(self.refresh(s) for s in target))

    async def refresh(self, symbol: str) -> None:
        """刷新单个 symbol。任一子项失败保留旧值，不中断其它字段。"""
        snap = self.ensure_symbol(symbol)
        tf = self._settings.llm.kline_interval
        limit = self._settings.llm.kline_lookback
        try:
            ticker, klines, funding = await asyncio.gather(
                self._client.fetch_ticker(symbol),
                self._client.fetch_ohlcv(symbol, tf, limit),
                self._client.fetch_funding_rate(symbol),
                return_exceptions=True,
            )
            if not isinstance(ticker, Exception) and ticker:
                snap.last_price = float(ticker.get("last") or snap.last_price)
                mark = ticker.get("mark") or (ticker.get("info") or {}).get("markPrice")
                snap.mark_price = float(mark) if mark else snap.last_price
                pct = ticker.get("percentage")
                if pct is not None:
                    snap.change_24h_pct = float(pct)
            else:
                logger.warning("ticker refresh failed {}: {}", symbol, ticker)

            if not isinstance(klines, Exception) and klines:
                snap.klines = klines
            else:
                logger.warning("ohlcv refresh failed {}: {}", symbol, klines)

            if not isinstance(funding, Exception) and funding:
                fr = funding.get("fundingRate")
                if fr is not None:
                    snap.funding_rate = float(fr)

            snap.updated_ms = int(time.time() * 1000)
        except Exception as e:  # 兜底：整体失败也不让主循环崩
            logger.exception("market refresh error {}: {}", symbol, e)
