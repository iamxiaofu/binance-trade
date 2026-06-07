"""执行层：把通过风控的决策落成真实/模拟订单。

职责：
- 精度规整（调用 filters.normalize_order）；不满足 minNotional/minQty → 返回拒单结果
- dry_run：只构造订单结构、记日志，不触碰交易所
- 真实下单：市价/限价开仓，可选附带 STOP_MARKET / TAKE_PROFIT_MARKET
- 限频与瞬时错误的指数退避重试
- 平仓 / flatten_all / cancel_all（供 kill-switch 与熔断使用）

所有方法返回标准化 dict（见 _result），交给 store.log_order 落库。
执行层不做任何风控判断——风控已在上游完成。
"""
from __future__ import annotations

import asyncio

import ccxt.async_support as ccxt
from loguru import logger

from src.config.schema import ExecutionConfig, Settings
from src.exchange.client import ExchangeClient
from src.exchange.filters import normalize_order
from src.exchange.orders import normalize_condition_order
from src.llm.schema import Action, TradeDecision


# 视为「已建/平仓成功」的状态：完全成交、部分成交、dry-run 模拟
_FILLED_STATES = ("filled", "partial", "dry_run")


def _result(
    *,
    symbol: str,
    kind: str,
    side: str,
    qty: float,
    price: float,
    notional: float,
    dry_run: bool,
    status: str,
    order_id: str = "",
    raw: dict | None = None,
    order_type: str = "market",
) -> dict:
    return {
        "symbol": symbol,
        "kind": kind,            # OPEN / CLOSE / SL / TP
        "side": side,            # buy / sell
        "order_type": order_type,
        "qty": qty,              # 实际成交数量（部分成交时为已成交量）
        "price": price,
        "notional": notional,
        "dry_run": dry_run,
        "status": status,        # filled / partial / placed / dry_run / rejected / error
        "id": order_id,
        "raw": raw or {},
        "opened": kind == "OPEN" and status in _FILLED_STATES,
        "closed": kind == "CLOSE" and status in _FILLED_STATES,
        "filled": status in _FILLED_STATES,
        "partial": status == "partial",
    }


def realized_pnl(*, side: str, entry_price: float, exit_price: float, qty: float) -> float:
    """估算平仓已实现盈亏(USDT，未计手续费/资金费)。

    side 为持仓方向 long/short。多头盈亏=(出场-入场)*量；空头相反。
    入参非法（价≤0 或量≤0）时返回 0，避免污染日亏累计。
    """
    if entry_price <= 0 or exit_price <= 0 or qty <= 0:
        return 0.0
    direction = 1.0 if side.lower() == "long" else -1.0
    return (exit_price - entry_price) * qty * direction


# OPEN_LONG → buy, OPEN_SHORT → sell
_OPEN_SIDE = {Action.OPEN_LONG: "buy", Action.OPEN_SHORT: "sell"}


class Executor:
    def __init__(self, client: ExchangeClient, settings: Settings):
        self._client = client
        self._settings = settings
        self._cfg: ExecutionConfig = settings.execution

    # ---------- 退避重试 ----------
    async def _with_retry(self, coro_factory, what: str):
        """对限频/网络瞬时错误指数退避重试。coro_factory 是无参 async 工厂。"""
        attempt = 0
        last_err: Exception | None = None
        while attempt <= self._cfg.max_order_retries:
            try:
                return await coro_factory()
            except (ccxt.RateLimitExceeded, ccxt.NetworkError, ccxt.DDoSProtection) as e:
                last_err = e
                wait = self._cfg.rate_limit_backoff ** attempt
                logger.warning("{} transient err (attempt {}): {}; backoff {:.1f}s",
                               what, attempt + 1, e, wait)
                await asyncio.sleep(wait)
                attempt += 1
            except ccxt.ExchangeError as e:
                # 业务错误（资金不足、参数错误）不重试
                logger.error("{} exchange error: {}", what, e)
                raise
        assert last_err is not None
        raise last_err

    # ---------- 成交解析 ----------
    @staticmethod
    def _parse_fill(order: dict, requested_qty: float, fallback_price: float) -> tuple[float, float, str]:
        """从 ccxt 订单解析(实际成交量, 成交均价, 状态)。

        市价单通常立即全成,但仍可能部分成交。以 ``filled`` 为准:
        - filled <= 0：交易所未回填(罕见)，保守按请求量当作全成，状态 filled
        - 0 < filled < requested：部分成交，状态 partial
        - filled >= requested：全成，状态 filled
        """
        filled = float(order.get("filled") or 0.0)
        avg = float(order.get("average") or order.get("price") or fallback_price)
        if filled <= 0:
            return requested_qty, avg, "filled"
        # 用 1e-12 容差吸收浮点误差
        if filled < requested_qty - 1e-12:
            return filled, avg, "partial"
        return filled, avg, "filled"

    # ---------- 开仓 ----------
    async def open_position(
        self,
        *,
        decision: TradeDecision,
        qty: float,
        price: float,
    ) -> dict:
        """按已计算的 qty 开仓。精度规整后下单；dry_run 时只模拟。"""
        symbol = decision.symbol
        side = _OPEN_SIDE[decision.action]
        f = self._client.filters(symbol)
        norm = normalize_order(qty=qty, price=price, f=f, is_market=True)
        if norm is None:
            logger.warning("[{}] normalize below min (qty={}, price={}) → reject", symbol, qty, price)
            return _result(symbol=symbol, kind="OPEN", side=side, qty=qty, price=price,
                           notional=0.0, dry_run=self._cfg.dry_run, status="rejected",
                           raw={"reason": "below minNotional/minQty"})
        q = float(norm.qty)
        notional = float(norm.notional)

        if self._cfg.dry_run:
            logger.info("[dry-run][{}] OPEN {} qty={} ~notional={:.2f}", symbol, side, q, notional)
            res = _result(symbol=symbol, kind="OPEN", side=side, qty=q, price=price,
                          notional=notional, dry_run=True, status="dry_run")
            res["leverage"] = decision.leverage
            res["margin"] = notional / decision.leverage if decision.leverage > 0 else 0.0
            return res

        # 真实下单前确保保证金模式+杠杆就位
        await self._client.setup_symbol(symbol, decision.leverage)
        order = await self._with_retry(
            lambda: self._client.create_order(symbol, side, q, "market"),
            f"open {symbol}",
        )
        fill_qty, avg_px, status = self._parse_fill(order, q, price)
        if status == "partial":
            logger.warning("[{}] OPEN partial fill {}/{} id={}", symbol, fill_qty, q, order.get("id"))
        else:
            logger.info("[{}] OPEN {} qty={} id={}", symbol, side, fill_qty, order.get("id"))
        res = _result(symbol=symbol, kind="OPEN", side=side, qty=fill_qty,
                      price=avg_px, notional=fill_qty * avg_px,
                      dry_run=False, status=status, order_id=str(order.get("id") or ""),
                      raw=order)
        res["leverage"] = decision.leverage
        res["margin"] = res["notional"] / decision.leverage if decision.leverage > 0 else 0.0
        return res

    # ---------- 止盈止损 ----------
    async def place_sl_tp(self, *, decision: TradeDecision, entry_price: float, qty: float) -> list[dict]:
        """挂 STOP_MARKET / TAKE_PROFIT_MARKET（reduceOnly）。dry_run 只模拟。"""
        if not self._cfg.attach_sl_tp:
            return []
        symbol = decision.symbol
        is_long = decision.action == Action.OPEN_LONG
        f = self._client.filters(symbol)

        def _trigger(pct: float, is_sl: bool) -> float:
            if is_sl:
                raw = entry_price * (1 - pct) if is_long else entry_price * (1 + pct)
            else:
                raw = entry_price * (1 + pct) if is_long else entry_price * (1 - pct)
            from src.exchange.filters import round_price
            return float(round_price(raw, f))

        specs = []
        if decision.stop_loss_pct > 0:
            specs.append(("SL", "STOP_MARKET", _trigger(decision.stop_loss_pct, True)))
        if decision.take_profit_pct > 0:
            specs.append(("TP", "TAKE_PROFIT_MARKET", _trigger(decision.take_profit_pct, False)))

        return await self.place_protection_orders(
            symbol=symbol,
            pos_side="long" if is_long else "short",
            qty=qty,
            specs=specs,
        )

    async def place_protection_orders(
        self,
        *,
        symbol: str,
        pos_side: str,
        qty: float,
        specs: list[tuple[str, str, float]],
    ) -> list[dict]:
        """按明确触发价补挂 reduce-only 保护条件单。"""
        close_side = "sell" if pos_side.lower() == "long" else "buy"
        results: list[dict] = []
        for kind, otype, trigger in specs:
            if self._cfg.dry_run:
                logger.info("[dry-run][{}] {} trigger={}", symbol, kind, trigger)
                results.append(_result(symbol=symbol, kind=kind, side=close_side, qty=qty,
                                       order_type=otype, price=trigger,
                                       notional=qty * trigger, dry_run=True, status="dry_run"))
                continue
            client_algo_id = self._protection_client_algo_id(
                symbol=symbol, kind=kind, side=close_side, qty=qty, trigger=trigger
            )
            try:
                order = await self._with_retry(
                    lambda otype=otype, trigger=trigger: self._client.create_order(
                        symbol, close_side, qty, otype.lower(), None,
                        {
                            "stopPrice": trigger,
                            "reduceOnly": True,
                            "clientAlgoId": client_algo_id,
                        },
                    ),
                    f"{kind} {symbol}",
                )
                results.append(_result(symbol=symbol, kind=kind, side=close_side, qty=qty,
                                       order_type=otype, price=trigger,
                                       notional=qty * trigger, dry_run=False, status="placed",
                                       order_id=str(order.get("id") or ""), raw=order))
            except Exception as e:
                recovered = await self._find_matching_condition_order(
                    symbol=symbol,
                    kind=kind,
                    side=close_side,
                    qty=qty,
                    trigger=trigger,
                    client_algo_id=client_algo_id,
                )
                if recovered is not None:
                    logger.warning(
                        "[{}] {} placement errored but matching live condition order exists id={}",
                        symbol, kind, recovered.get("id"),
                    )
                    results.append(_result(
                        symbol=symbol,
                        kind=kind,
                        side=close_side,
                        qty=qty,
                        order_type=otype,
                        price=trigger,
                        notional=qty * trigger,
                        dry_run=False,
                        status="placed",
                        order_id=str(recovered.get("id") or ""),
                        raw=recovered.get("raw") or recovered,
                    ))
                    continue
                logger.error("[{}] place {} failed: {}", symbol, kind, e)
                results.append(_result(symbol=symbol, kind=kind, side=close_side, qty=qty,
                                       order_type=otype, price=trigger, notional=0.0,
                                       dry_run=False, status="error",
                                       raw={"error": str(e), "clientAlgoId": client_algo_id}))
        return results

    @staticmethod
    def _protection_client_algo_id(
        *,
        symbol: str,
        kind: str,
        side: str,
        qty: float,
        trigger: float,
    ) -> str:
        import hashlib

        raw = f"{symbol}:{kind}:{side}:{qty:.12g}:{trigger:.12g}"
        digest = hashlib.sha1(raw.encode("ascii")).hexdigest()[:22]
        return f"bt-{digest}"

    async def _find_matching_condition_order(
        self,
        *,
        symbol: str,
        kind: str,
        side: str,
        qty: float,
        trigger: float,
        client_algo_id: str,
    ) -> dict | None:
        try:
            orders = await self._client.fetch_open_condition_orders(symbol)
        except Exception as e:
            logger.warning("[{}] condition order recovery query failed: {}", symbol, e)
            return None
        qty_tol = max(abs(qty) * 1e-6, 1e-12)
        px_tol = max(abs(trigger) * 1e-8, 1e-8)
        for raw in orders:
            order = normalize_condition_order(raw)
            info = raw.get("info") or {}
            if client_algo_id and order.get("client_algo_id") == client_algo_id:
                order["raw"] = raw
                return order
            if order.get("kind") != kind:
                continue
            if (order.get("side") or "").lower() != side:
                continue
            if abs(float(order.get("qty") or 0.0) - qty) > qty_tol:
                continue
            if abs(float(order.get("trigger_price") or 0.0) - trigger) > px_tol:
                continue
            if str(order.get("status") or "") != "placed":
                continue
            if not order.get("reduce_only"):
                continue
            order["raw"] = raw
            if client_algo_id and info.get("clientAlgoId") != client_algo_id:
                logger.warning(
                    "[{}] recovered {} by qty/trigger with different clientAlgoId {}",
                    symbol, kind, info.get("clientAlgoId"),
                )
            return order
        return None

    # ---------- 平仓 ----------
    async def close_position(self, position: dict) -> dict:
        """市价平掉单个持仓（reduceOnly）。position 为 ccxt position dict。

        返回结果额外带 ``entry_price`` / ``pos_side``，供上层计算已实现盈亏。
        """
        symbol = (position.get("symbol") or "").replace("/USDT:USDT", "USDT")
        contracts = abs(float(position.get("contracts") or 0))
        pos_side = (position.get("side") or "").lower()  # long/short
        entry = float(position.get("entryPrice") or 0)
        if contracts == 0:
            return _result(symbol=symbol, kind="CLOSE", side="", qty=0.0, price=0.0,
                           notional=0.0, dry_run=self._cfg.dry_run, status="rejected",
                           raw={"reason": "no position"})
        close_side = "sell" if pos_side == "long" else "buy"
        mark = float(position.get("markPrice") or position.get("entryPrice") or 0)

        if self._cfg.dry_run:
            logger.info("[dry-run][{}] CLOSE {} qty={}", symbol, close_side, contracts)
            res = _result(symbol=symbol, kind="CLOSE", side=close_side, qty=contracts,
                          price=mark, notional=contracts * mark, dry_run=True, status="dry_run")
            res["entry_price"] = entry
            res["pos_side"] = pos_side
            return res
        order = await self._with_retry(
            lambda: self._client.create_order(
                symbol, close_side, contracts, "market", None, {"reduceOnly": True}
            ),
            f"close {symbol}",
        )
        fill_qty, avg_px, status = self._parse_fill(order, contracts, mark)
        logger.info("[{}] CLOSE {} qty={} id={} status={}",
                    symbol, close_side, fill_qty, order.get("id"), status)
        res = _result(symbol=symbol, kind="CLOSE", side=close_side, qty=fill_qty,
                      price=avg_px, notional=fill_qty * avg_px,
                      dry_run=False, status=status, order_id=str(order.get("id") or ""),
                      raw=order)
        res["entry_price"] = entry
        res["pos_side"] = pos_side
        return res

    # ---------- 批量（熔断 / kill-switch）----------
    async def flatten_all(self) -> list[dict]:
        """平掉所有持仓。"""
        results: list[dict] = []
        positions = await self._client.fetch_positions(self._settings.symbols)
        for p in positions:
            try:
                results.append(await self.close_position(p))
            except Exception as e:
                logger.error("flatten_all close failed: {}", e)
        return results

    async def cancel_all_orders(self) -> None:
        if self._cfg.dry_run:
            logger.info("[dry-run] cancel_all_orders skipped")
            return
        await self._client.cancel_all_orders()
        await self._client.cancel_all_condition_orders()
