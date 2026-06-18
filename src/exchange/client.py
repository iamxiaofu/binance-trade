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
        self._markets: dict[str, Any] = {}

    @property
    def raw(self) -> ccxt.binanceusdm:
        return self._exchange

    async def load_markets(self, reload: bool = False) -> None:
        """加载市场元数据并解析每个交易标的的精度过滤器。"""
        markets = await self._exchange.load_markets(reload)
        self._markets = markets
        self._markets_loaded = True
        for sym in self._settings.symbols:
            self._filters[sym] = self._filters_from_markets(sym, markets)
        logger.info("markets loaded, filters for {} symbols", len(self._filters))

    def _filters_from_markets(self, symbol: str, markets: dict[str, Any]) -> SymbolFilters:
        symbol = symbol.upper().strip()
        market = markets.get(self._to_ccxt_symbol(symbol))
        if market is None:
            raise RuntimeError(f"交易所不支持 symbol：{symbol}")
        return SymbolFilters.from_ccxt_market(market)

    async def ensure_symbol(self, symbol: str) -> SymbolFilters:
        """验证交易所支持该币种，并确保本地已缓存其 filters。"""
        symbol = symbol.upper().strip()
        if not self._markets_loaded:
            await self.load_markets()
        if symbol not in self._filters:
            self._filters[symbol] = self._filters_from_markets(symbol, self._markets)
            logger.info("market filters loaded for dynamic symbol {}", symbol)
        return self._filters[symbol]

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
            message = str(e).lower()
            if "no need to change margin type" in message or "-4046" in message:
                logger.debug("set_margin_mode({}) already configured: {}", symbol, e)
            else:
                logger.warning("set_margin_mode({}) failed: {}", symbol, e)
                raise
        try:
            await self._exchange.set_leverage(leverage, ccxt_sym)
        except ccxt.ExchangeError as e:
            logger.warning("set_leverage({}, {}) failed: {}", symbol, leverage, e)
            raise

    async def validate_account_mode(self) -> None:
        """Fail closed for account modes unsupported by the execution model."""
        position_mode = await self._exchange.fapiPrivateGetPositionSideDual()
        dual = str(position_mode.get("dualSidePosition", "")).lower() == "true"
        if dual:
            raise RuntimeError("hedge position mode is unsupported; switch Binance to one-way mode")
        multi_assets = await self._exchange.fapiPrivateGetMultiAssetsMargin()
        enabled = str(multi_assets.get("multiAssetsMargin", "")).lower() == "true"
        if enabled:
            raise RuntimeError("multi-assets margin mode is unsupported")

    # ---------- 查询 ----------
    async def fetch_server_time(self) -> int:
        return await self._exchange.fetch_time()

    async def fetch_balance(self) -> dict[str, Any]:
        bal = await self._exchange.fetch_balance()
        return bal

    async def fetch_income_history(self, start_ms: int, limit: int = 1000) -> list[dict[str, Any]]:
        """Fetch Binance's realized PnL, funding and commission ledger."""
        rows = await self._exchange.fapiPrivateGetIncome(
            {"startTime": int(start_ms), "limit": min(max(int(limit), 1), 1000)}
        )
        return list(rows or [])

    async def fetch_available_margin(self, quote: str = "USDT") -> float:
        bal = await self._exchange.fetch_balance()
        free = (bal.get("free") or {}).get(quote)
        return float(free) if free is not None else 0.0

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[dict]:
        active = symbols or list(self._filters) or self._settings.symbols
        syms = [self._to_ccxt_symbol(s) for s in active]
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

    async def fetch_order_book(self, symbol: str, limit: int = 5) -> dict:
        return await self._exchange.fetch_order_book(self._to_ccxt_symbol(symbol), limit)

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

    async def fetch_order(self, symbol: str, order_id: str, params: dict | None = None) -> dict:
        return await self._exchange.fetch_order(
            order_id,
            self._to_ccxt_symbol(symbol),
            params or {},
        )

    async def fetch_order_by_client_id(self, symbol: str, client_order_id: str) -> dict | None:
        """Recover an ambiguously acknowledged regular order by client ID."""
        try:
            raw = await self._exchange.fapiPrivateGetOrder({
                "symbol": symbol,
                "origClientOrderId": client_order_id,
            })
        except ccxt.OrderNotFound:
            return None
        return {
            "id": str(raw.get("orderId") or ""),
            "clientOrderId": str(raw.get("clientOrderId") or client_order_id),
            "status": str(raw.get("status") or "").lower(),
            "filled": float(raw.get("executedQty") or 0.0),
            "average": float(raw.get("avgPrice") or 0.0),
            "amount": float(raw.get("origQty") or 0.0),
            "info": raw,
        }

    async def fetch_order_trades(self, symbol: str, order_id: str, limit: int = 100) -> list[dict]:
        return await self._exchange.fetch_my_trades(
            self._to_ccxt_symbol(symbol),
            since=None,
            limit=limit,
            params={"orderId": order_id},
        )

    async def fetch_my_trades(
        self,
        symbol: str,
        *,
        since: int | None = None,
        until: int | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        params = {"endTime": int(until)} if until is not None else {}
        return await self._exchange.fetch_my_trades(
            self._to_ccxt_symbol(symbol),
            since=since,
            limit=min(max(int(limit), 1), 1000),
            params=params,
        )

    async def cancel_order(
        self,
        symbol: str,
        order_id: str,
        params: dict | None = None,
    ) -> Any:
        return await self._exchange.cancel_order(
            order_id,
            self._to_ccxt_symbol(symbol),
            params or {},
        )

    async def cancel_all_orders(
        self,
        symbol: str | None = None,
        symbols: list[str] | None = None,
    ) -> Any:
        if symbol:
            return await self._exchange.cancel_all_orders(self._to_ccxt_symbol(symbol))
        if symbols is None:
            symbols = list(self._filters) or list(self._settings.symbols)
        results = []
        for s in symbols:
            try:
                results.append(await self._exchange.cancel_all_orders(self._to_ccxt_symbol(s)))
            except ccxt.ExchangeError as e:
                logger.warning("cancel_all_orders({}) failed: {}", s, e)
        return results

    async def cancel_condition_order(
        self,
        symbol: str,
        order_id: str,
        *,
        client_algo_id: str = "",
    ) -> Any:
        """Cancel one USD-M conditional algo order.

        ccxt 的统一 cancel_order 会按 symbol+algoId 走条件单接口；Binance 文档
        的裸接口只要求 algoId/clientAlgoId。这里保留两条路径，兼容 ccxt 与交易所
        testnet/mainnet 的实现差异。
        """
        ccxt_sym = self._to_ccxt_symbol(symbol)
        errors: list[Exception] = []
        try:
            return await self._exchange.cancel_order(
                order_id, ccxt_sym, params={"conditional": True}
            )
        except ccxt.ExchangeError as e:
            errors.append(e)

        requests: list[dict[str, Any]] = []
        if client_algo_id:
            requests.append({"clientAlgoId": client_algo_id})
            requests.append({"symbol": symbol, "clientAlgoId": client_algo_id})
        if order_id:
            requests.append({"algoId": order_id})
            requests.append({"symbol": symbol, "algoId": order_id})
        for request in requests:
            try:
                return await self._exchange.fapiPrivateDeleteAlgoOrder(request)
            except ccxt.ExchangeError as e:
                errors.append(e)
        raise errors[-1]

    async def cancel_all_condition_orders(
        self,
        symbol: str | None = None,
        symbols: list[str] | None = None,
    ) -> Any:
        """Cancel open USD-M conditional algo orders only."""
        symbols = [symbol] if symbol else (symbols or list(self._filters) or list(self._settings.symbols))
        results = []
        for s in symbols:
            if not s:
                continue
            ccxt_sym = self._to_ccxt_symbol(s)
            try:
                results.append(
                    await self._exchange.cancel_all_orders(
                        ccxt_sym, params={"conditional": True}
                    )
                )
            except ccxt.ExchangeError as e:
                logger.warning("cancel_all_condition_orders({}) ccxt path failed: {}", s, e)
            try:
                results.append(
                    await self._exchange.fapiPrivateDeleteAlgoOpenOrders({"symbol": s})
                )
            except ccxt.ExchangeError as e:
                logger.warning("cancel_all_condition_orders({}) direct path failed: {}", s, e)
        return results

    async def close(self) -> None:
        await self._exchange.close()
