"""Binance USDT-M 永续合约客户端：基于 ccxt 异步封装。

只做「执行与查询」，不含任何交易决策。负责：
- 初始化 testnet/mainnet 连接，加载市场元数据
- 设置保证金模式(ISOLATED/CROSS)与杠杆
- 下单 / 撤单 / 查持仓 / 查余额
- 服务器时间同步（recvWindow 由 ccxt options 处理）

并发与限频：交给 ccxt 的 enableRateLimit；重试与退避在 execution 层做。
"""
from __future__ import annotations

from typing import Any

import ccxt.async_support as ccxt
from loguru import logger

from src.config.schema import Credentials, MarginMode, Settings
from src.exchange.filters import SymbolFilters


class ExchangeClient:
    """ccxt binanceusdm 的异步封装。使用后必须 await close()。"""

    def __init__(self, settings: Settings, creds: Credentials):
        self._settings = settings
        self._exchange: ccxt.binanceusdm = ccxt.binanceusdm(
            {
                "apiKey": creds.binance_api_key,
                "secret": creds.binance_api_secret,
                "enableRateLimit": True,
                "options": {
                    "defaultType": "future",
                    "recvWindow": settings.execution.recv_window,
                    "adjustForTimeDifference": True,  # ccxt 自动用服务器时间校准
                    # 币安合约 testnet(testnet.binancefuture.com)仍可用，但 ccxt>=4.5
                    # 默认对其下单/签名接口抛 NotSupported。该官方开关解除拦截。
                    # 主网不走 sandbox，此选项无副作用。
                    "disableFuturesSandboxWarning": True,
                },
            }
        )
        if not settings.is_mainnet:
            self._exchange.set_sandbox_mode(True)
        self._filters: dict[str, SymbolFilters] = {}
        self._markets_loaded = False

    @property
    def raw(self) -> ccxt.binanceusdm:
        return self._exchange

    async def load_markets(self, reload: bool = False) -> None:
        """加载市场元数据并解析每个交易标的的精度过滤器。"""
        markets = await self._exchange.load_markets(reload)
        self._markets_loaded = True
        for sym in self._settings.symbols:
            market = markets.get(self._to_ccxt_symbol(sym))
            if market is None:
                raise RuntimeError(f"交易所不支持 symbol：{sym}")
            self._filters[sym] = SymbolFilters.from_ccxt_market(market)
        logger.info("markets loaded, filters for {} symbols", len(self._filters))

    def filters(self, symbol: str) -> SymbolFilters:
        if symbol not in self._filters:
            raise RuntimeError(f"{symbol} 过滤器未加载，请先 load_markets()")
        return self._filters[symbol]

    @staticmethod
    def _to_ccxt_symbol(symbol: str) -> str:
        """BTCUSDT → BTC/USDT:USDT（ccxt 永续统一符号）。"""
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        return f"{base}/USDT:USDT"

    # ---------- 账户配置 ----------
    async def setup_symbol(self, symbol: str, leverage: int) -> None:
        """开仓前确保保证金模式与杠杆就位。无持仓时才允许改保证金模式。"""
        ccxt_sym = self._to_ccxt_symbol(symbol)
        margin_mode = self._settings.account.margin_mode.value
        try:
            await self._exchange.set_margin_mode(margin_mode, ccxt_sym)
        except ccxt.ExchangeError as e:
            # "No need to change margin type" 等幂等错误可忽略
            logger.debug("set_margin_mode({}) skipped: {}", symbol, e)
        try:
            await self._exchange.set_leverage(leverage, ccxt_sym)
        except ccxt.ExchangeError as e:
            logger.warning("set_leverage({}, {}) failed: {}", symbol, leverage, e)
            raise

    # ---------- 查询 ----------
    async def fetch_server_time(self) -> int:
        return await self._exchange.fetch_time()

    async def fetch_balance(self) -> dict[str, Any]:
        bal = await self._exchange.fetch_balance()
        return bal

    async def fetch_available_margin(self, quote: str = "USDT") -> float:
        bal = await self._exchange.fetch_balance()
        free = (bal.get("free") or {}).get(quote)
        return float(free) if free is not None else 0.0

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[dict]:
        syms = [self._to_ccxt_symbol(s) for s in (symbols or self._settings.symbols)]
        positions = await self._exchange.fetch_positions(syms)
        # 只保留有实际仓位的
        return [p for p in positions if p.get("contracts") and float(p["contracts"]) != 0]

    async def fetch_open_orders(self, symbol: str | None = None) -> list[dict]:
        sym = self._to_ccxt_symbol(symbol) if symbol else None
        return await self._exchange.fetch_open_orders(sym)

    async def fetch_open_condition_orders(self, symbol: str) -> list[dict]:
        return await self._exchange.fetch_open_orders(
            self._to_ccxt_symbol(symbol), params={"conditional": True}
        )

    async def fetch_condition_orders(self, symbol: str, limit: int = 20) -> list[dict]:
        return await self._exchange.fetch_orders(
            self._to_ccxt_symbol(symbol), limit=limit, params={"conditional": True}
        )

    # ---------- 行情 ----------
    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        return await self._exchange.fetch_ohlcv(
            self._to_ccxt_symbol(symbol), timeframe=timeframe, limit=limit
        )

    async def fetch_ticker(self, symbol: str) -> dict:
        return await self._exchange.fetch_ticker(self._to_ccxt_symbol(symbol))

    async def fetch_funding_rate(self, symbol: str) -> dict:
        return await self._exchange.fetch_funding_rate(self._to_ccxt_symbol(symbol))

    # ---------- 交易 ----------
    async def create_order(
        self,
        symbol: str,
        side: str,            # "buy" | "sell"
        amount: float,
        order_type: str = "market",
        price: float | None = None,
        params: dict | None = None,
    ) -> dict:
        return await self._exchange.create_order(
            self._to_ccxt_symbol(symbol), order_type, side, amount, price, params or {}
        )

    async def cancel_all_orders(self, symbol: str | None = None) -> Any:
        if symbol:
            return await self._exchange.cancel_all_orders(self._to_ccxt_symbol(symbol))
        results = []
        for s in self._settings.symbols:
            try:
                results.append(await self._exchange.cancel_all_orders(self._to_ccxt_symbol(s)))
            except ccxt.ExchangeError as e:
                logger.warning("cancel_all_orders({}) failed: {}", s, e)
        return results

    async def close(self) -> None:
        await self._exchange.close()
