"""Web 看板专用的行情数据源：testnet + mainnet 双源，REST 拉历史 + WS 推实时。

设计原则：
- 与交易主进程完全解耦。这里只读公开行情，绝不下单、绝不用交易密钥做行情查询。
- mainnet 行情是公开数据，无需 API key；testnet 行情走 sandbox。
- 用 ccxt.pro 的 watch_ohlcv 订阅币安 WS，实时推最后一根 K 线（亚秒级），
  比 REST 轮询更接近交易所体验，且不受 REST 限频困扰。
- 单例 + 懒加载：每个 source 只建一个 ccxt 客户端，多个浏览器连接共享。

source 取值："mainnet" | "testnet"。
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, Literal

import ccxt.pro as ccxtpro
from loguru import logger

Source = Literal["mainnet", "testnet"]


def _to_ccxt_symbol(symbol: str) -> str:
    """BTCUSDT → BTC/USDT:USDT（ccxt 永续统一符号）。"""
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return f"{base}/USDT:USDT"


class MarketFeed:
    """单个数据源（mainnet 或 testnet）的 ccxt.pro 客户端封装。"""

    def __init__(self, source: Source):
        self.source = source
        self._ex: ccxtpro.binanceusdm = ccxtpro.binanceusdm(
            {
                "enableRateLimit": True,
                "options": {"defaultType": "future"},
            }
        )
        if source == "testnet":
            # 公开行情无需 key；sandbox 仅切换到 testnet 行情端点
            self._ex.set_sandbox_mode(True)
        self._markets_loaded = False
        self._load_lock = asyncio.Lock()

    async def _ensure_markets(self) -> None:
        if self._markets_loaded:
            return
        async with self._load_lock:
            if not self._markets_loaded:
                await self._ex.load_markets()
                self._markets_loaded = True

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int
    ) -> list[list[float]]:
        """REST 拉历史 K 线（首屏 + 周期切换用，可一次拉满一天）。"""
        await self._ensure_markets()
        return await self._ex.fetch_ohlcv(
            _to_ccxt_symbol(symbol), timeframe=timeframe, limit=limit
        )

    async def fetch_ticker(self, symbol: str) -> dict:
        await self._ensure_markets()
        return await self._ex.fetch_ticker(_to_ccxt_symbol(symbol))

    async def watch_ticker(self, symbol: str) -> AsyncIterator[dict]:
        """WS 订阅最新 ticker。"""
        await self._ensure_markets()
        ccxt_sym = _to_ccxt_symbol(symbol)
        while True:
            yield await self._ex.watch_ticker(ccxt_sym)

    async def watch_ohlcv(
        self, symbol: str, timeframe: str
    ) -> AsyncIterator[list[float]]:
        """WS 订阅：每次推送 yield 最新（未收盘）的那根 K 线 [ts,o,h,l,c,v]。"""
        await self._ensure_markets()
        ccxt_sym = _to_ccxt_symbol(symbol)
        while True:
            ohlcv = await self._ex.watch_ohlcv(ccxt_sym, timeframe)
            if ohlcv:
                yield ohlcv[-1]

    async def close(self) -> None:
        try:
            await self._ex.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("market feed close ({}) error: {}", self.source, e)


class MarketFeedRegistry:
    """按 source 缓存 MarketFeed 单例，供 web 多连接共享。"""

    def __init__(self) -> None:
        self._feeds: dict[Source, MarketFeed] = {}
        self._lock = asyncio.Lock()

    async def get(self, source: Source) -> MarketFeed:
        if source not in ("mainnet", "testnet"):
            raise ValueError(f"未知行情源：{source}")
        async with self._lock:
            if source not in self._feeds:
                self._feeds[source] = MarketFeed(source)
                logger.info("market feed created: {}", source)
            return self._feeds[source]

    async def close_all(self) -> None:
        for feed in self._feeds.values():
            await feed.close()
        self._feeds.clear()
