"""执行层：把通过风控的决策落成交易所订单。

职责：
- 精度规整（调用 filters.normalize_order）；不满足 minNotional/minQty → 返回拒单结果
- 市价/限价开仓，可选附带 STOP_MARKET / TAKE_PROFIT_MARKET
- 限频与瞬时错误的指数退避重试
- 平仓 / flatten_all / cancel_all（供 kill-switch 与熔断使用）

所有方法返回标准化 dict（见 _result），交给 store.log_order 落库。
执行层不做任何风控判断——风控已在上游完成。
"""
from __future__ import annotations

import asyncio

import ccxt.async_support as ccxt
from loguru import logger

from src.config.schema import ExecutionConfig, ExecutionMode, MakerUnfilledAction, Settings
from src.exchange.client import ExchangeClient
from src.exchange.filters import normalize_order
from src.exchange.orders import normalize_condition_order
from src.execution.policy import ExecutionPolicy
from src.llm.schema import Action, TradeDecision


# 视为「已建/平仓成功」的状态：完全成交、部分成交
_FILLED_STATES = ("filled", "partial")


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
    execution_mode: str = "",
    time_in_force: str = "",
    requested_qty: float = 0.0,
    requested_price: float = 0.0,
    limit_price: float = 0.0,
    remaining_qty: float = 0.0,
    liquidity: str = "",
    fee: float = 0.0,
    fee_asset: str = "",
    client_order_id: str = "",
) -> dict:
    filled_qty = qty if status in _FILLED_STATES else 0.0
    return {
        "symbol": symbol,
        "kind": kind,            # OPEN / CLOSE / SL / TP
        "side": side,            # buy / sell
        "order_type": order_type,
        "qty": qty,              # 实际成交数量（部分成交时为已成交量）
        "price": price,
        "notional": notional,
        "dry_run": dry_run,
        "status": status,        # filled / partial / placed / rejected / error
        "id": order_id,
        "raw": raw or {},
        "execution_mode": execution_mode,
        "time_in_force": time_in_force,
        "requested_qty": requested_qty or qty,
        "filled_qty": filled_qty,
        "remaining_qty": remaining_qty,
        "requested_price": requested_price,
        "limit_price": limit_price,
        "avg_price": price if filled_qty > 0 else 0.0,
        "liquidity": liquidity,
        "fee": fee,
        "fee_asset": fee_asset,
        "client_order_id": client_order_id,
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
        self._policy = ExecutionPolicy(self._cfg)

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
    def _safe_float(value: object) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _safe_str(value: object) -> str:
        return str(value or "")

    @staticmethod
    def _raw_info(order: dict) -> dict:
        info = order.get("info")
        return info if isinstance(info, dict) else {}

    @classmethod
    def _order_status(cls, order: dict) -> str:
        info = cls._raw_info(order)
        raw = cls._safe_str(order.get("status") or info.get("status")).lower()
        return {
            "closed": "filled",
            "filled": "filled",
            "partially_filled": "partial",
            "partial": "partial",
            "open": "placed",
            "new": "placed",
            "canceled": "canceled",
            "cancelled": "canceled",
            "expired": "expired",
            "rejected": "rejected",
        }.get(raw, raw or "unknown")

    @classmethod
    def _filled_amount(cls, order: dict) -> float:
        info = cls._raw_info(order)
        return cls._safe_float(
            order.get("filled")
            or info.get("executedQty")
            or info.get("cumQty")
            or info.get("z")
        )

    @classmethod
    def _avg_price(cls, order: dict, fallback_price: float) -> float:
        info = cls._raw_info(order)
        avg = cls._safe_float(
            order.get("average")
            or info.get("avgPrice")
            or info.get("ap")
            or order.get("price")
            or info.get("price")
        )
        return avg if avg > 0 else fallback_price

    @classmethod
    def _parse_fill(
        cls,
        order: dict,
        requested_qty: float,
        fallback_price: float,
        *,
        assume_filled_if_missing: bool = True,
    ) -> tuple[float, float, str]:
        """从 ccxt 订单解析(实际成交量, 成交均价, 状态)。

        市价单通常立即全成,但仍可能部分成交。以 ``filled`` 为准:
        - filled <= 0：交易所未回填(罕见)，保守按请求量当作全成，状态 filled
        - 0 < filled < requested：部分成交，状态 partial
        - filled >= requested：全成，状态 filled
        """
        filled = cls._filled_amount(order)
        avg = cls._avg_price(order, fallback_price)
        if filled <= 0:
            if assume_filled_if_missing:
                return requested_qty, avg, "filled"
            status = cls._order_status(order)
            if status == "filled":
                return requested_qty, avg, "filled"
            return 0.0, avg, "placed" if status in ("unknown", "placed") else status
        # 用 1e-12 容差吸收浮点误差
        if filled < requested_qty - 1e-12:
            return filled, avg, "partial"
        return filled, avg, "filled"

    @staticmethod
    def _client_order_id(*, symbol: str, kind: str, side: str) -> str:
        import hashlib
        import time

        raw = f"{symbol}:{kind}:{side}:{time.time_ns()}"
        digest = hashlib.sha1(raw.encode("ascii")).hexdigest()[:22]
        return f"bt-{digest}"

    @classmethod
    def _order_client_id(cls, order: dict, fallback: str = "") -> str:
        info = cls._raw_info(order)
        return cls._safe_str(
            order.get("clientOrderId")
            or order.get("client_order_id")
            or info.get("clientOrderId")
            or info.get("origClientOrderId")
            or fallback
        )

    @classmethod
    def _fee_summary(cls, order: dict, trades: list[dict] | None = None) -> tuple[float, str, str]:
        fee_total = 0.0
        fee_asset = ""
        liquidity = ""

        def _add_fee(fee_obj: object) -> None:
            nonlocal fee_total, fee_asset
            if not isinstance(fee_obj, dict):
                return
            cost = cls._safe_float(fee_obj.get("cost"))
            if cost:
                fee_total += abs(cost)
            currency = cls._safe_str(fee_obj.get("currency"))
            if currency and not fee_asset:
                fee_asset = currency

        info = cls._raw_info(order)
        if info.get("maker") is not None:
            liquidity = "maker" if str(info.get("maker")).lower() == "true" else "taker"

        trade_rows = trades or []
        if not trade_rows:
            _add_fee(order.get("fee"))
            for fee_obj in order.get("fees") or []:
                _add_fee(fee_obj)

        for trade in trade_rows:
            _add_fee(trade.get("fee"))
            for fee_obj in trade.get("fees") or []:
                _add_fee(fee_obj)
            trade_info = trade.get("info") if isinstance(trade.get("info"), dict) else {}
            commission = cls._safe_float(trade_info.get("commission"))
            if commission:
                fee_total += abs(commission)
            asset = cls._safe_str(trade_info.get("commissionAsset"))
            if asset and not fee_asset:
                fee_asset = asset
            maker = trade_info.get("maker")
            if maker is not None:
                liquidity = "maker" if str(maker).lower() == "true" else "taker"
            taker_or_maker = cls._safe_str(trade.get("takerOrMaker"))
            if taker_or_maker and not liquidity:
                liquidity = taker_or_maker

        return fee_total, fee_asset, liquidity

    async def _fetch_order_trades_safe(self, symbol: str, order_id: str) -> list[dict]:
        if not order_id or not hasattr(self._client, "fetch_order_trades"):
            return []
        try:
            return await self._client.fetch_order_trades(symbol, order_id)
        except Exception as e:
            logger.debug("[{}] fetch order trades {} skipped: {}", symbol, order_id, e)
            return []

    # ---------- 开仓 ----------
    async def open_position(
        self,
        *,
        decision: TradeDecision,
        qty: float,
        price: float,
    ) -> dict:
        """按已计算的 qty 开仓。精度规整后下单。"""
        mode = self._cfg.entry_mode or ExecutionMode.MARKET_TAKER
        if mode is ExecutionMode.MARKET_TAKER:
            return await self._open_market_position(decision=decision, qty=qty, price=price)
        return await self._open_maker_position(decision=decision, qty=qty, price=price, mode=mode)

    def _slippage_limit_bps(self, symbol: str) -> float:
        """按 symbol 取市价单最大允许滑点（bps）。先 per_symbol 覆盖，再 default。"""
        sym = (symbol or "").upper()
        per = (self._cfg.market_slippage_bps_per_symbol or {}).get(sym)
        if per is not None:
            return float(per)
        return float(self._cfg.market_slippage_bps)

    async def _preflight_market_slippage(
        self,
        *,
        symbol: str,
        side: str,         # "buy" / "sell"
        ref_price: float,  # 决策参考价（用于估算偏差）
        qty: float,
    ) -> tuple[bool, float, str]:
        """市价单预检：用盘口估算成交价 vs ref_price 的偏差是否超阈值。

        返回 (ok, est_impact_price, reason)。
        - ok=True  允许下市价单
        - ok=False 应拒单（reason 解释）
        估算方法：以 ref_price 一侧的对手盘深度累计 qty，得到加权均价 est_impact。
        卖单看 bids（吃买盘），买单看 asks（吃卖盘）。
        """
        if ref_price <= 0 or qty <= 0:
            return True, ref_price, ""
        try:
            book = await self._client.fetch_order_book(symbol, limit=20)
        except Exception as e:
            logger.warning("[{}] preflight orderbook failed (allow): {}", symbol, e)
            return True, ref_price, ""
        levels = (book.get("bids") if side == "sell" else book.get("asks")) or []
        if not levels:
            return True, ref_price, ""
        # 累计 qty 拿加权均价
        remaining = qty
        cost = 0.0
        for lvl in levels:
            px = float(lvl[0] or 0.0)
            sz = float(lvl[1] or 0.0)
            if px <= 0 or sz <= 0:
                continue
            take = min(sz, remaining)
            cost += take * px
            remaining -= take
            if remaining <= 0:
                break
        if remaining > 0:
            # 盘口深度不够：可能严重滑点，但保守起见 allow（市价单至少会成交部分）
            logger.warning(
                "[{}] preflight: book depth insufficient qty={} remaining={}",
                symbol, qty, remaining,
            )
            est_impact = cost / max(qty - remaining, 1e-9)
        else:
            est_impact = cost / qty
        # 偏差：卖单 impact <= ref_price（吃买盘会更低），买单 impact >= ref_price
        if side == "sell":
            bps = (ref_price - est_impact) / ref_price * 10000.0
        else:
            bps = (est_impact - ref_price) / ref_price * 10000.0
        limit = self._slippage_limit_bps(symbol)
        if bps > limit:
            return False, est_impact, (
                f"slippage {bps:.1f}bps > limit {limit:.1f}bps "
                f"(est_impact={est_impact:.4f} ref={ref_price:.4f})"
            )
        return True, est_impact, ""

    async def _open_market_position(
        self,
        *,
        decision: TradeDecision,
        qty: float,
        price: float,
    ) -> dict:
        symbol = decision.symbol
        side = _OPEN_SIDE[decision.action]
        f = self._client.filters(symbol)
        norm = normalize_order(qty=qty, price=price, f=f, is_market=True)
        if norm is None:
            logger.warning("[{}] normalize below min (qty={}, price={}) → reject", symbol, qty, price)
            return _result(symbol=symbol, kind="OPEN", side=side, qty=qty, price=price,
                           notional=0.0, dry_run=False, status="rejected",
                           raw={"reason": "below minNotional/minQty"},
                           execution_mode=ExecutionMode.MARKET_TAKER.value,
                           requested_qty=qty, requested_price=price)
        # 市价单滑点预检：参考价 (price) 偏离盘口估算成交价过远则拒单，
        # 避免 FALLBACK_MARKET 兜底时拿到不合理价格。
        ok, _est, reason = await self._preflight_market_slippage(
            symbol=symbol, side=side, ref_price=price, qty=float(norm.qty),
        )
        if not ok:
            logger.warning(
                "[{}] market OPEN rejected by slippage guard: {}",
                symbol, reason,
            )
            return _result(
                symbol=symbol, kind="OPEN", side=side, qty=float(norm.qty), price=price,
                notional=0.0, dry_run=False, status="rejected",
                raw={"reason": "slippage_exceeded", "detail": reason},
                execution_mode=ExecutionMode.MARKET_TAKER.value,
                requested_qty=float(norm.qty), requested_price=price,
            )
        q = float(norm.qty)

        # 下单前确保保证金模式+杠杆就位
        await self._client.setup_symbol(symbol, decision.leverage)
        client_order_id = self._client_order_id(symbol=symbol, kind="OPEN", side=side)
        order = await self._with_retry(
            lambda: self._client.create_order(
                symbol, side, q, "market", None, {"newClientOrderId": client_order_id}
            ),
            f"open {symbol}",
        )
        fill_qty, avg_px, status = self._parse_fill(order, q, price)
        order_id = str(order.get("id") or (order.get("info") or {}).get("orderId") or "")
        trades = await self._fetch_order_trades_safe(symbol, order_id)
        fee, fee_asset, liquidity = self._fee_summary(order, trades)
        liquidity = liquidity or "taker"
        if status == "partial":
            logger.warning("[{}] OPEN partial fill {}/{} id={}", symbol, fill_qty, q, order.get("id"))
        else:
            logger.info("[{}] OPEN {} qty={} id={}", symbol, side, fill_qty, order.get("id"))
        res = _result(symbol=symbol, kind="OPEN", side=side, qty=fill_qty,
                      price=avg_px, notional=fill_qty * avg_px,
                      dry_run=False, status=status, order_id=order_id,
                      raw=order, execution_mode=ExecutionMode.MARKET_TAKER.value,
                      requested_qty=q, requested_price=price, remaining_qty=max(q - fill_qty, 0.0),
                      liquidity=liquidity, fee=fee, fee_asset=fee_asset,
                      client_order_id=self._order_client_id(order, client_order_id))
        res["leverage"] = decision.leverage
        res["margin"] = res["notional"] / decision.leverage if decision.leverage > 0 else 0.0
        return res

    async def _open_maker_position(
        self,
        *,
        decision: TradeDecision,
        qty: float,
        price: float,
        mode: ExecutionMode,
    ) -> dict:
        symbol = decision.symbol
        side = _OPEN_SIDE[decision.action]
        f = self._client.filters(symbol)
        await self._client.setup_symbol(symbol, decision.leverage)

        last_rejected: dict | None = None
        attempts = self._cfg.maker_max_requotes + 1
        # 累计跨 attempt 的真实成交：B2/B3 修复：避免 race 下「attempt 1
        # 已部分成交但 fetch_order 返回 -2013」导致那部分成交被后续 attempt 覆盖。
        cumulative_filled = 0.0
        cumulative_cost = 0.0
        cumulative_fee = 0.0
        cumulative_fee_asset = ""
        winning_attempt: dict | None = None
        attempt_order_ids: list[str] = []
        for attempt in range(attempts):
            quote = await self._policy.maker_quote(
                client=self._client,
                symbol=symbol,
                side=side,
                fallback_price=price,
                filters=f,
            )
            norm = normalize_order(qty=qty, price=quote.price, f=f, is_market=False)
            if norm is None:
                logger.warning(
                    "[{}] maker normalize below min (qty={}, price={}) -> reject",
                    symbol, qty, quote.price,
                )
                return _result(
                    symbol=symbol,
                    kind="OPEN",
                    side=side,
                    qty=qty,
                    price=quote.price,
                    notional=0.0,
                    dry_run=False,
                    status="rejected",
                    raw={"reason": "below minNotional/minQty", "maker_quote": quote.__dict__},
                    order_type="limit",
                    execution_mode=mode.value,
                    time_in_force=self._cfg.maker_time_in_force,
                    requested_qty=qty,
                    requested_price=price,
                    limit_price=quote.price,
                )
            q = float(norm.qty)
            limit_price = float(norm.price or quote.price)
            client_order_id = self._client_order_id(symbol=symbol, kind="OPEN", side=side)
            params = {
                "timeInForce": self._cfg.maker_time_in_force,
                "newClientOrderId": client_order_id,
            }
            order = await self._with_retry(
                lambda: self._client.create_order(symbol, side, q, "limit", limit_price, params),
                f"maker open {symbol}",
            )
            order_id = str(order.get("id") or (order.get("info") or {}).get("orderId") or "")
            if order_id:
                attempt_order_ids.append(order_id)
            logger.info(
                "[{}] maker OPEN {} qty={} price={} id={} attempt={}/{}",
                symbol, side, q, limit_price, order_id, attempt + 1, attempts,
            )
            observed = await self._wait_maker_fill(
                symbol=symbol,
                order_id=order_id,
                created_order=order,
                requested_qty=q,
                fallback_price=limit_price,
            )
            fill_qty, avg_px, status = self._parse_fill(
                observed, q, limit_price, assume_filled_if_missing=False
            )
            if fill_qty > 0:
                # 当前 attempt 有成交：先撤剩余，累加到跨 attempt 总额。
                cancel_raw = None
                if status == "partial":
                    cancel_raw = await self._cancel_regular_order_safe(symbol, order_id)
                    logger.warning(
                        "[{}] maker OPEN partial fill {}/{} id={}, canceled rest",
                        symbol, fill_qty, q, order_id,
                    )
                trades = await self._fetch_order_trades_safe(symbol, order_id)
                fee, fee_asset, liquidity = self._fee_summary(observed, trades)
                liquidity = liquidity or "maker"
                # 累加跨 attempt 真实成交
                attempt_cost = fill_qty * (avg_px if avg_px > 0 else limit_price)
                cumulative_filled += fill_qty
                cumulative_cost += attempt_cost
                cumulative_fee += fee
                if fee_asset and not cumulative_fee_asset:
                    cumulative_fee_asset = fee_asset
                winning_attempt = {
                    "fill_qty": fill_qty,
                    "avg_px": avg_px,
                    "status": status,
                    "order": observed,
                    "initial_order": order,
                    "cancel_remaining": cancel_raw,
                    "maker_quote": quote.__dict__,
                    "client_order_id": self._order_client_id(observed, client_order_id),
                    "liquidity": liquidity,
                }
                # 已达到计划量，提前收尾（不再发后续 attempt）
                if cumulative_filled >= q - 1e-12:
                    break
                # 否则继续下一 attempt 补足
                continue

            cancel_raw = await self._cancel_regular_order_safe(symbol, order_id)
            last_rejected = {
                "order": observed,
                "initial_order": order,
                "cancel_remaining": cancel_raw,
                "maker_quote": quote.__dict__,
                "reason": "maker unfilled",
            }

        # 收尾：若任一 attempt 有过成交，按累计成交出 partial/filled 结果
        if winning_attempt is not None and cumulative_filled > 0:
            cum_avg = (cumulative_cost / cumulative_filled) if cumulative_filled > 0 else winning_attempt["avg_px"]
            cum_status = "filled" if cumulative_filled >= q - 1e-12 else "partial"
            raw = {
                "order": winning_attempt["order"],
                "initial_order": winning_attempt["initial_order"],
                "cancel_remaining": winning_attempt["cancel_remaining"],
                "maker_quote": winning_attempt["maker_quote"],
                "cumulative_filled": cumulative_filled,
                "cumulative_cost": cumulative_cost,
                "attempt_order_ids": attempt_order_ids,
            }
            res = _result(
                symbol=symbol,
                kind="OPEN",
                side=side,
                qty=cumulative_filled,
                price=cum_avg if cum_avg > 0 else winning_attempt["avg_px"],
                notional=cumulative_cost,
                dry_run=False,
                status=cum_status,
                order_id=str(winning_attempt["order"].get("id") or ""),
                raw=raw,
                order_type="limit",
                execution_mode=mode.value,
                time_in_force=self._cfg.maker_time_in_force,
                requested_qty=q,
                requested_price=price,
                limit_price=limit_price,
                remaining_qty=max(q - cumulative_filled, 0.0),
                liquidity=winning_attempt["liquidity"],
                fee=cumulative_fee,
                fee_asset=cumulative_fee_asset,
                client_order_id=winning_attempt["client_order_id"],
            )
            res["leverage"] = decision.leverage
            res["margin"] = res["notional"] / decision.leverage if decision.leverage > 0 else 0.0
            return res

        # 全部 attempt 都没看到 fill，再做一次 myTrades 全局对账（B3 修复）：
        # 万一 _wait_maker_fill 的 _recover_via_my_trades 没命中（极少见：API 临时
        # 把 myTrades 也搞挂了），这里兜底再查一次。
        residual = await self._reconcile_maker_residual_fills(
            symbol=symbol, order_ids=attempt_order_ids, fallback_price=limit_price,
        )
        if residual is not None and residual["filled"] > 0:
            res = _result(
                symbol=symbol,
                kind="OPEN",
                side=side,
                qty=residual["filled"],
                price=residual["avg_price"],
                notional=residual["cost"],
                dry_run=False,
                status="partial" if residual["filled"] < q - 1e-12 else "filled",
                order_id=str(residual["order_id"] or ""),
                raw={"reason": "residual myTrades reconcile", "residual": residual},
                order_type="limit",
                execution_mode=mode.value,
                time_in_force=self._cfg.maker_time_in_force,
                requested_qty=q,
                requested_price=price,
                limit_price=limit_price,
                remaining_qty=max(q - residual["filled"], 0.0),
                liquidity="maker",
                fee=residual["fee"],
                fee_asset=residual["fee_asset"],
            )
            res["leverage"] = decision.leverage
            res["margin"] = res["notional"] / decision.leverage if decision.leverage > 0 else 0.0
            return res

        if mode is ExecutionMode.MAKER_FIRST and (
            self._cfg.maker_unfilled_action is MakerUnfilledAction.FALLBACK_MARKET
        ):
            logger.warning("[{}] maker unfilled, falling back to MARKET_TAKER", symbol)
            return await self._open_market_position(decision=decision, qty=qty, price=price)

        logger.warning("[{}] maker OPEN canceled unfilled after {} attempts", symbol, attempts)
        return _result(
            symbol=symbol,
            kind="OPEN",
            side=side,
            qty=0.0,
            price=price,
            notional=0.0,
            dry_run=False,
            status="canceled",
            order_id=str((last_rejected or {}).get("order", {}).get("id") or ""),
            raw=last_rejected or {"reason": "maker unfilled"},
            order_type="limit",
            execution_mode=mode.value,
            time_in_force=self._cfg.maker_time_in_force,
            requested_qty=qty,
            requested_price=price,
        )

    async def _reconcile_maker_residual_fills(
        self,
        *,
        symbol: str,
        order_ids: list[str],
        fallback_price: float,
    ) -> dict | None:
        """MAKER 全失败收尾时，对所有尝试过的 order_id 再查一次 myTrades。

        兜底用：正常路径已由 ``_wait_maker_fill._recover_via_my_trades`` 覆盖。
        这里只处理 _recover_via_my_trades 因网络瞬时错误全失败的情况。
        返回 {order_id, filled, cost, avg_price, fee, fee_asset} 或 None。
        """
        if not order_ids or not hasattr(self._client, "fetch_order_trades"):
            return None
        total_filled = 0.0
        total_cost = 0.0
        total_fee = 0.0
        fee_asset = ""
        first_order_id = ""
        for oid in order_ids:
            try:
                trades = await self._client.fetch_order_trades(symbol, oid)
            except Exception as e:
                logger.warning(
                    "[{}] residual myTrades query for {} failed: {}", symbol, oid, e,
                )
                continue
            if not first_order_id and oid:
                first_order_id = oid
            for t in trades:
                amount = self._safe_float(t.get("amount") or 0.0)
                price = self._safe_float(t.get("price") or 0.0)
                total_filled += amount
                total_cost += amount * price
                fee = (t.get("fee") or {}) if isinstance(t.get("fee"), dict) else {}
                cost = self._safe_float(fee.get("cost"))
                if cost:
                    total_fee += abs(cost)
                if not fee_asset:
                    fee_asset = self._safe_str(fee.get("currency"))
        if total_filled <= 0:
            return None
        avg_price = (total_cost / total_filled) if total_filled > 0 else fallback_price
        return {
            "order_id": first_order_id,
            "filled": total_filled,
            "cost": total_cost,
            "avg_price": avg_price,
            "fee": total_fee,
            "fee_asset": fee_asset,
        }

    async def _wait_maker_fill(
        self,
        *,
        symbol: str,
        order_id: str,
        created_order: dict,
        requested_qty: float,
        fallback_price: float,
    ) -> dict:
        """轮询 maker 订单直到完成/取消/超时。

        重要：fetch_order 抛 -2013（OrderNotFound）不一定是「完全未成交」——Binance
        在订单被自动取消（例如 GTX 触发、IOC 部分成交后撤单）时也会返回这个错。
        此时必须回退到 ``fetch_order_trades(order_id)`` 查 ``myTrades`` 拿真实
        成交数据，再把 ``observed`` 改写成「已成交 + 已取消」的状态返回，避免把
        部分成交的仓位丢掉。

        硬上限：即使所有 fetch 都 hang 也不让 maker 轮询把策略循环卡死
        （``maker_timeout_seconds * 2``，下限 5s）。
        """
        deadline = asyncio.get_running_loop().time() + self._cfg.maker_timeout_seconds
        observed = created_order
        hard_cap = max(self._cfg.maker_timeout_seconds * 2.0, 5.0)
        started = asyncio.get_running_loop().time()
        while True:
            fill_qty, _avg_px, status = self._parse_fill(
                observed,
                requested_qty,
                fallback_price,
                assume_filled_if_missing=False,
            )
            if fill_qty > 0 or status in ("filled", "canceled", "expired", "rejected"):
                return observed
            now = asyncio.get_running_loop().time()
            if now >= deadline:
                return observed
            if now - started > hard_cap:
                logger.error(
                    "[{}] maker wait hard cap {}s exceeded for order {}; aborting wait",
                    symbol, hard_cap, order_id,
                )
                return observed
            await asyncio.sleep(min(self._cfg.maker_poll_seconds, max(deadline - now, 0.0)))
            if not order_id or not hasattr(self._client, "fetch_order"):
                continue
            try:
                observed = await self._client.fetch_order(symbol, order_id)
            except ccxt.OrderNotFound as e:
                # 关键修复：订单在 fetch 之前已不可见（被取消/已成交后清理），
                # 但 myTrades 仍能查到成交。回退到成交历史做一次最终判定。
                recovered = await self._recover_via_my_trades(
                    symbol=symbol, order_id=order_id, requested_qty=requested_qty,
                    fallback_price=fallback_price,
                )
                if recovered is not None:
                    observed = recovered
                    logger.warning(
                        "[{}] maker order {} disappeared ({}); recovered fills from myTrades",
                        symbol, order_id, e,
                    )
                else:
                    logger.warning(
                        "[{}] maker order {} disappeared ({}); no myTrades fill; treat as unfilled",
                        symbol, order_id, e,
                    )
                    return observed
            except (ccxt.RateLimitExceeded, ccxt.NetworkError, ccxt.DDoSProtection) as e:
                logger.warning(
                    "[{}] fetch maker order {} transient error: {}; retry within deadline",
                    symbol, order_id, e,
                )
                continue
            except ccxt.ExchangeError as e:
                # 其它业务错误（账户/参数等），不再重试这一单，避免后续 attempt 误判
                logger.warning(
                    "[{}] fetch maker order {} exchange error: {}; aborting wait",
                    symbol, order_id, e,
                )
                return observed

    async def _recover_via_my_trades(
        self,
        *,
        symbol: str,
        order_id: str,
        requested_qty: float,
        fallback_price: float,
    ) -> dict | None:
        """fetch_order 失败时回退到 myTrades 拿真实成交。

        返回值：构造好的 order dict（status 反映 filled/canceled+部分成交），
        便于上层 ``_parse_fill`` 解析出 fill_qty>0；或 None（确实没有成交）。
        """
        if not order_id or not hasattr(self._client, "fetch_order_trades"):
            return None
        try:
            trades = await self._client.fetch_order_trades(symbol, order_id)
        except Exception as e:
            logger.warning("[{}] myTrades recovery for {} failed: {}", symbol, order_id, e)
            return None
        if not trades:
            return None
        filled = sum(self._safe_float(t.get("amount") or 0.0) for t in trades)
        if filled <= 0:
            return None
        # 重新计算加权均价与手续费
        total_cost = 0.0
        fee_total = 0.0
        fee_asset = ""
        for t in trades:
            amount = self._safe_float(t.get("amount") or 0.0)
            price = self._safe_float(t.get("price") or 0.0)
            total_cost += amount * price
            fee = (t.get("fee") or {}) if isinstance(t.get("fee"), dict) else {}
            cost = self._safe_float(fee.get("cost"))
            if cost:
                fee_total += abs(cost)
            if not fee_asset:
                fee_asset = self._safe_str(fee.get("currency"))
        avg_price = (total_cost / filled) if filled > 0 else fallback_price
        # 模拟 ccxt order dict 形态，让 _parse_fill 走 partial/filled 分支
        return {
            "id": order_id,
            "symbol": symbol,
            "amount": requested_qty,
            "price": fallback_price,
            "average": avg_price if avg_price > 0 else fallback_price,
            "filled": filled,
            "remaining": max(0.0, requested_qty - filled),
            "status": "filled" if filled >= requested_qty - 1e-12 else "canceled",
            "info": {
                "orderId": order_id,
                "status": "PARTIALLY_FILLED" if filled < requested_qty - 1e-12 else "FILLED",
                "executedQty": str(filled),
                "cumulativeQuoteQty": str(total_cost),
                "avgPrice": str(avg_price),
            },
            "fee": {"cost": fee_total, "currency": fee_asset},
            "trades": trades,
        }

    async def _cancel_regular_order_safe(self, symbol: str, order_id: str) -> dict | None:
        if not order_id or not hasattr(self._client, "cancel_order"):
            return None
        try:
            return await self._client.cancel_order(symbol, order_id)
        except ccxt.OrderNotFound:
            return {"status": "not_found"}
        except Exception as e:
            logger.warning("[{}] cancel regular order {} failed: {}", symbol, order_id, e)
            return {"error": str(e)}

    # ---------- 止盈止损 ----------
    async def place_sl_tp(self, *, decision: TradeDecision, entry_price: float, qty: float) -> list[dict]:
        """挂 STOP_MARKET / TAKE_PROFIT_MARKET（reduceOnly）。"""
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
    async def close_position(
        self,
        position: dict,
        *,
        mode: ExecutionMode | str | None = None,
    ) -> dict:
        """平掉单个持仓（reduceOnly）。position 为 ccxt position dict。

        返回结果额外带 ``entry_price`` / ``pos_side``，供上层计算已实现盈亏。
        """
        selected = ExecutionMode(mode or self._cfg.normal_exit_mode or ExecutionMode.MARKET_TAKER)
        if selected is ExecutionMode.MARKET_TAKER:
            return await self._close_market_position(position, mode=selected)
        return await self._close_maker_position(position, mode=selected)

    async def _close_market_position(self, position: dict, *, mode: ExecutionMode) -> dict:
        symbol = (position.get("symbol") or "").replace("/USDT:USDT", "USDT")
        contracts = abs(float(position.get("contracts") or 0))
        pos_side = (position.get("side") or "").lower()  # long/short
        entry = float(position.get("entryPrice") or 0)
        if contracts == 0:
            return _result(symbol=symbol, kind="CLOSE", side="", qty=0.0, price=0.0,
                           notional=0.0, dry_run=False, status="rejected",
                           raw={"reason": "no position"})
        close_side = "sell" if pos_side == "long" else "buy"
        mark = float(position.get("markPrice") or position.get("entryPrice") or 0)
        # 市价平仓滑点预检：用 mark 价作为 ref，盘口估算平仓冲击价超阈值则拒单。
        if mark > 0:
            ok, _est, reason = await self._preflight_market_slippage(
                symbol=symbol, side=close_side, ref_price=mark, qty=contracts,
            )
            if not ok:
                logger.warning(
                    "[{}] market CLOSE rejected by slippage guard: {}",
                    symbol, reason,
                )
                return _result(
                    symbol=symbol, kind="CLOSE", side=close_side, qty=contracts, price=mark,
                    notional=0.0, dry_run=False, status="rejected",
                    raw={"reason": "slippage_exceeded", "detail": reason},
                    execution_mode=mode.value,
                    requested_qty=contracts, requested_price=mark,
                )
        client_order_id = self._client_order_id(symbol=symbol, kind="CLOSE", side=close_side)

        order = await self._with_retry(
            lambda: self._client.create_order(
                symbol, close_side, contracts, "market", None,
                {"reduceOnly": True, "newClientOrderId": client_order_id}
            ),
            f"close {symbol}",
        )
        fill_qty, avg_px, status = self._parse_fill(order, contracts, mark)
        order_id = str(order.get("id") or (order.get("info") or {}).get("orderId") or "")
        trades = await self._fetch_order_trades_safe(symbol, order_id)
        fee, fee_asset, liquidity = self._fee_summary(order, trades)
        liquidity = liquidity or "taker"
        logger.info("[{}] CLOSE {} qty={} id={} status={}",
                    symbol, close_side, fill_qty, order.get("id"), status)
        res = _result(symbol=symbol, kind="CLOSE", side=close_side, qty=fill_qty,
                      price=avg_px, notional=fill_qty * avg_px,
                      dry_run=False, status=status, order_id=order_id,
                      raw=order, execution_mode=mode.value,
                      requested_qty=contracts, requested_price=mark,
                      remaining_qty=max(contracts - fill_qty, 0.0),
                      liquidity=liquidity, fee=fee, fee_asset=fee_asset,
                      client_order_id=self._order_client_id(order, client_order_id))
        res["entry_price"] = entry
        res["pos_side"] = pos_side
        return res

    async def _close_maker_position(self, position: dict, *, mode: ExecutionMode) -> dict:
        symbol = (position.get("symbol") or "").replace("/USDT:USDT", "USDT")
        contracts = abs(float(position.get("contracts") or 0))
        pos_side = (position.get("side") or "").lower()
        entry = float(position.get("entryPrice") or 0)
        mark = float(position.get("markPrice") or position.get("entryPrice") or 0)
        if contracts == 0:
            return _result(symbol=symbol, kind="CLOSE", side="", qty=0.0, price=0.0,
                           notional=0.0, dry_run=False, status="rejected",
                           raw={"reason": "no position"}, execution_mode=mode.value)
        close_side = "sell" if pos_side == "long" else "buy"
        f = self._client.filters(symbol)
        last_rejected: dict | None = None
        attempts = self._cfg.maker_max_requotes + 1
        for attempt in range(attempts):
            quote = await self._policy.maker_quote(
                client=self._client,
                symbol=symbol,
                side=close_side,
                fallback_price=mark,
                filters=f,
            )
            norm = normalize_order(qty=contracts, price=quote.price, f=f, is_market=False)
            if norm is None:
                return _result(
                    symbol=symbol, kind="CLOSE", side=close_side, qty=contracts,
                    price=quote.price, notional=0.0, dry_run=False, status="rejected",
                    raw={"reason": "below minNotional/minQty", "maker_quote": quote.__dict__},
                    order_type="limit", execution_mode=mode.value,
                    time_in_force=self._cfg.maker_time_in_force,
                    requested_qty=contracts, requested_price=mark, limit_price=quote.price,
                )
            q = float(norm.qty)
            limit_price = float(norm.price or quote.price)
            client_order_id = self._client_order_id(symbol=symbol, kind="CLOSE", side=close_side)
            params = {
                "reduceOnly": True,
                "timeInForce": self._cfg.maker_time_in_force,
                "newClientOrderId": client_order_id,
            }
            order = await self._with_retry(
                lambda: self._client.create_order(symbol, close_side, q, "limit", limit_price, params),
                f"maker close {symbol}",
            )
            order_id = str(order.get("id") or (order.get("info") or {}).get("orderId") or "")
            observed = await self._wait_maker_fill(
                symbol=symbol,
                order_id=order_id,
                created_order=order,
                requested_qty=q,
                fallback_price=limit_price,
            )
            fill_qty, avg_px, status = self._parse_fill(
                observed, q, limit_price, assume_filled_if_missing=False
            )
            if fill_qty > 0:
                cancel_raw = None
                if status == "partial":
                    cancel_raw = await self._cancel_regular_order_safe(symbol, order_id)
                trades = await self._fetch_order_trades_safe(symbol, order_id)
                fee, fee_asset, liquidity = self._fee_summary(observed, trades)
                liquidity = liquidity or "maker"
                raw = {
                    "order": observed,
                    "initial_order": order,
                    "cancel_remaining": cancel_raw,
                    "maker_quote": quote.__dict__,
                }
                res = _result(
                    symbol=symbol,
                    kind="CLOSE",
                    side=close_side,
                    qty=fill_qty,
                    price=avg_px,
                    notional=fill_qty * avg_px,
                    dry_run=False,
                    status=status,
                    order_id=order_id,
                    raw=raw,
                    order_type="limit",
                    execution_mode=mode.value,
                    time_in_force=self._cfg.maker_time_in_force,
                    requested_qty=q,
                    requested_price=mark,
                    limit_price=limit_price,
                    remaining_qty=max(q - fill_qty, 0.0),
                    liquidity=liquidity,
                    fee=fee,
                    fee_asset=fee_asset,
                    client_order_id=self._order_client_id(observed, client_order_id),
                )
                res["entry_price"] = entry
                res["pos_side"] = pos_side
                logger.info(
                    "[{}] maker CLOSE {} qty={} id={} status={}",
                    symbol, close_side, fill_qty, order_id, status,
                )
                return res

            cancel_raw = await self._cancel_regular_order_safe(symbol, order_id)
            last_rejected = {
                "order": observed,
                "initial_order": order,
                "cancel_remaining": cancel_raw,
                "maker_quote": quote.__dict__,
                "reason": "maker unfilled",
                "attempt": attempt + 1,
            }

        if mode is ExecutionMode.MAKER_FIRST and (
            self._cfg.maker_unfilled_action is MakerUnfilledAction.FALLBACK_MARKET
        ):
            logger.warning("[{}] maker close unfilled, falling back to MARKET_TAKER", symbol)
            return await self._close_market_position(position, mode=ExecutionMode.MARKET_TAKER)

        res = _result(
            symbol=symbol,
            kind="CLOSE",
            side=close_side,
            qty=0.0,
            price=mark,
            notional=0.0,
            dry_run=False,
            status="canceled",
            order_id=str((last_rejected or {}).get("order", {}).get("id") or ""),
            raw=last_rejected or {"reason": "maker unfilled"},
            order_type="limit",
            execution_mode=mode.value,
            time_in_force=self._cfg.maker_time_in_force,
            requested_qty=contracts,
            requested_price=mark,
        )
        res["entry_price"] = entry
        res["pos_side"] = pos_side
        return res

    # ---------- 批量（熔断 / kill-switch）----------
    async def flatten_all(self, symbols: list[str] | None = None) -> list[dict]:
        """平掉所有持仓。"""
        results: list[dict] = []
        positions = await self._client.fetch_positions(symbols or self._settings.symbols)
        for p in positions:
            try:
                results.append(
                    await self.close_position(p, mode=self._cfg.emergency_exit_mode)
                )
            except Exception as e:
                logger.error("flatten_all close failed: {}", e)
        return results

    async def cancel_all_orders(self, symbols: list[str] | None = None) -> None:
        await self._client.cancel_all_orders(symbols=symbols)
        await self._client.cancel_all_condition_orders(symbols=symbols)
