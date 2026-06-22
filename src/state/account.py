"""Single-writer account projection driven by private events and REST snapshots."""
from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from loguru import logger

from src.exchange.events import ExchangeEvent
from src.exchange.orders import normalize_condition_order, normalize_open_order
from src.exchange.positions import normalize_position, normalize_symbol
from src.state.runtime import RuntimeState
from src.store.repo import Store


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    positions: dict[str, dict[str, Any]]
    open_orders: dict[str, list[dict[str, Any]]]
    balances: dict[str, dict[str, Any]]
    stream_status: str
    updated_at_ms: int


class AccountStateCoordinator:
    def __init__(self, store: Store, runtime: RuntimeState, quote_asset: str = "USDT"):
        self._store = store
        self._runtime = runtime
        self._quote = quote_asset
        self._queue: asyncio.Queue[ExchangeEvent] = asyncio.Queue(maxsize=20000)
        self._task: asyncio.Task | None = None
        self._positions: dict[str, dict[str, Any]] = {}
        self._orders: dict[str, dict[str, Any]] = {}
        self._balances: dict[str, dict[str, Any]] = {}
        self._entity_versions: dict[str, int] = {}
        self._stream_status = "STARTING"
        self._updated_at_ms = 0
        self._changed = asyncio.Condition()

    async def start(self) -> None:
        if self._task is None:
            for event in await self._store.pending_exchange_events():
                try:
                    await self._apply(event)
                    await self._store.mark_exchange_event_applied(event.event_key)
                except Exception as exc:
                    await self._store.mark_exchange_event_failed(event.event_key, str(exc))
            self._task = asyncio.create_task(self._run(), name="account-state-coordinator")

    @property
    def started(self) -> bool:
        return self._task is not None

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def submit(self, event: ExchangeEvent) -> None:
        await self._queue.put(event)

    async def drain(self) -> None:
        await self._queue.join()

    async def wait_for_order(self, client_order_id: str, timeout: float) -> dict[str, Any] | None:
        def _find() -> dict[str, Any] | None:
            for order in self._orders.values():
                if client_order_id in (
                    order.get("client_order_id"), order.get("client_algo_id")
                ):
                    return deepcopy(order)
            return None

        found = _find()
        if found is not None:
            return found
        try:
            async with asyncio.timeout(timeout):
                async with self._changed:
                    while True:
                        await self._changed.wait()
                        found = _find()
                        if found is not None:
                            return found
        except TimeoutError:
            return None

    def snapshot(self) -> AccountSnapshot:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for order in self._orders.values():
            if order.get("status") in ("placed", "new", "open", "working", "partial"):
                grouped.setdefault(order["symbol"], []).append(deepcopy(order))
        return AccountSnapshot(
            positions=deepcopy(self._positions),
            open_orders=grouped,
            balances=deepcopy(self._balances),
            stream_status=self._stream_status,
            updated_at_ms=self._updated_at_ms,
        )

    async def set_stream_health(self, health: dict[str, Any]) -> None:
        self._stream_status = str(health.get("status") or "UNKNOWN")
        await self._store.record_stream_health(health)

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                inserted = await self._store.record_exchange_event(event)
                if inserted:
                    await self._apply(event)
                    await self._store.mark_exchange_event_applied(event.event_key)
            except Exception as exc:
                logger.exception("account event apply failed {}: {}", event.event_type, exc)
                await self._store.mark_exchange_event_failed(event.event_key, str(exc))
            finally:
                self._queue.task_done()

    def _newer(self, entity: str, ts_ms: int) -> bool:
        if ts_ms and ts_ms < self._entity_versions.get(entity, 0):
            return False
        if ts_ms:
            self._entity_versions[entity] = ts_ms
        return True

    async def _apply(self, event: ExchangeEvent) -> None:
        if event.event_type == "REST_ACCOUNT_SNAPSHOT":
            await self._apply_rest_snapshot(event)
        elif event.event_type == "ACCOUNT_UPDATE":
            await self._apply_account_update(event)
        elif event.event_type == "ORDER_TRADE_UPDATE":
            await self._apply_order_update(event)
        elif event.event_type == "ALGO_UPDATE":
            await self._apply_algo_update(event)
        self._updated_at_ms = max(event.received_at_ms, self._updated_at_ms)
        self._sync_runtime()
        async with self._changed:
            self._changed.notify_all()

    async def _apply_rest_snapshot(self, event: ExchangeEvent) -> None:
        positions: dict[str, dict[str, Any]] = {}
        for raw in event.payload.get("positions") or []:
            pos = normalize_position(raw)
            if pos["contracts"] > 0:
                positions[pos["symbol"]] = raw
        old_position_shape = {
            symbol: (normalize_position(raw)["side"], normalize_position(raw)["contracts"])
            for symbol, raw in self._positions.items()
        }
        new_position_shape = {
            symbol: (normalize_position(raw)["side"], normalize_position(raw)["contracts"])
            for symbol, raw in positions.items()
        }
        if old_position_shape != new_position_shape and (old_position_shape or new_position_shape):
            await self._store.record_state_drift(
                entity_type="positions",
                entity_key="account",
                reason=str(event.payload.get("reason") or "REST reconciliation"),
                projection=old_position_shape,
                rest=new_position_shape,
            )
        self._positions = positions
        old_order_shape = {
            key: (row.get("symbol"), row.get("status"), row.get("filled_qty"))
            for key, row in self._orders.items()
        }
        next_orders: dict[str, dict[str, Any]] = {}
        for raw in event.payload.get("open_orders") or []:
            raw_type = str(raw.get("type") or (raw.get("info") or {}).get("type") or "").upper()
            normalized = (
                normalize_condition_order(raw)
                if (
                    (raw.get("info") or {}).get("algoId")
                    or raw.get("clientAlgoId")
                    or raw_type.startswith(("STOP", "TAKE_PROFIT"))
                )
                else normalize_open_order(raw)
            )
            key = str(normalized.get("id") or normalized.get("client_order_id")
                      or normalized.get("client_algo_id") or "")
            if key:
                next_orders[key] = normalized
        new_order_shape = {
            key: (row.get("symbol"), row.get("status"), row.get("filled_qty"))
            for key, row in next_orders.items()
        }
        if old_order_shape != new_order_shape and (old_order_shape or new_order_shape):
            await self._store.record_state_drift(
                entity_type="orders",
                entity_key="account",
                reason=str(event.payload.get("reason") or "REST reconciliation"),
                projection=old_order_shape,
                rest=new_order_shape,
            )
        self._orders = next_orders
        balance = event.payload.get("balance") or {}
        for asset, total in (balance.get("total") or {}).items():
            self._balances[asset] = {
                "asset": asset,
                "wallet_balance": float(total or 0),
                "available_balance": float((balance.get("free") or {}).get(asset) or 0),
                "ts_ms": event.event_time_ms,
            }
        await self._store.replace_live_account(
            positions=list(self._positions.values()),
            orders=list(self._orders.values()),
            balances=list(self._balances.values()),
            source="rest",
            ts_ms=event.event_time_ms,
        )

    async def _apply_account_update(self, event: ExchangeEvent) -> None:
        account = event.payload.get("a") or {}
        for bal in account.get("B") or []:
            asset = str(bal.get("a") or "")
            if not asset or not self._newer(f"balance:{asset}", event.transaction_time_ms):
                continue
            row = {
                "asset": asset,
                "wallet_balance": float(bal.get("wb") or 0),
                # ACCOUNT_UPDATE exposes cross-wallet balance (cw), not the
                # authoritative available balance. Preserve the latest REST value.
                "available_balance": float(
                    (self._balances.get(asset) or {}).get("available_balance") or 0
                ),
                "ts_ms": event.transaction_time_ms,
            }
            self._balances[asset] = row
            await self._store.upsert_live_balance(row, "stream")
            if asset == self._quote and row["wallet_balance"] > 0:
                self._runtime.update_equity(row["wallet_balance"])
        for raw in account.get("P") or []:
            symbol = normalize_symbol(raw.get("s"))
            if not symbol or not self._newer(f"position:{symbol}", event.transaction_time_ms):
                continue
            previous = self._positions.get(symbol) or {}
            previous_info = (
                previous.get("info") if isinstance(previous.get("info"), dict) else {}
            )
            position = {
                **previous,
                "symbol": symbol,
                "positionAmt": raw.get("pa"),
                "entryPrice": raw.get("ep"),
                "unRealizedProfit": raw.get("up"),
                "marginType": raw.get("mt"),
                "isolatedWallet": raw.get("iw"),
                "positionSide": raw.get("ps"),
                "updateTime": event.transaction_time_ms,
                # ACCOUNT_UPDATE is sparse. Preserve the latest REST-only fields
                # (mark, liquidation, leverage, margin details) until the next
                # authoritative REST snapshot, while applying fresh stream values.
                "info": {**previous_info, **raw},
            }
            pos = normalize_position(position)
            if pos["contracts"] > 0:
                self._positions[symbol] = position
                await self._store.upsert_live_position(position, "stream")
            else:
                self._positions.pop(symbol, None)
                await self._store.delete_live_position(symbol)

    async def _apply_order_update(self, event: ExchangeEvent) -> None:
        raw = event.payload.get("o") or {}
        order = normalize_open_order({"info": {
            "orderId": raw.get("i"), "symbol": raw.get("s"), "side": raw.get("S"),
            "orderType": raw.get("o"), "origQty": raw.get("q"), "price": raw.get("p"),
            "executedQty": raw.get("z"), "avgPrice": raw.get("ap"), "status": raw.get("X"),
            "timeInForce": raw.get("f"), "reduceOnly": raw.get("R"),
            "clientOrderId": raw.get("c"), "updateTime": event.transaction_time_ms,
        }})
        key = order["id"] or order["client_order_id"]
        if key and self._newer(f"order:{key}", event.transaction_time_ms):
            self._orders[key] = order
            await self._store.upsert_live_order(order, "regular", "stream")
            self._runtime.mark_order_event(order["symbol"])

    async def _apply_algo_update(self, event: ExchangeEvent) -> None:
        raw = event.payload.get("o") or event.payload.get("a") or {}
        order = normalize_condition_order({"info": {
            **raw,
            "algoId": raw.get("aid") or raw.get("algoId"),
            "clientAlgoId": raw.get("caid") or raw.get("clientAlgoId"),
            "symbol": raw.get("s") or raw.get("symbol"),
            "side": raw.get("S") or raw.get("side"),
            "orderType": raw.get("o") or raw.get("orderType"),
            "quantity": raw.get("q") or raw.get("quantity"),
            "price": raw.get("p") or raw.get("price"),
            "triggerPrice": raw.get("sp") or raw.get("triggerPrice") or raw.get("tp"),
            "algoStatus": raw.get("X") or raw.get("algoStatus"),
            "reduceOnly": raw.get("R") if raw.get("R") is not None else raw.get("reduceOnly"),
            "closePosition": (
                raw.get("cp") if raw.get("cp") is not None else raw.get("closePosition")
            ),
            "updateTime": event.transaction_time_ms,
        }})
        key = order["id"] or order["client_algo_id"]
        if key and self._newer(f"algo:{key}", event.transaction_time_ms):
            self._orders[key] = order
            await self._store.upsert_live_order(order, "algo", "stream")
            self._runtime.mark_order_event(order["symbol"])

    def _sync_runtime(self) -> None:
        self._runtime.positions = deepcopy(self._positions)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for order in self._orders.values():
            if order.get("status") in ("placed", "new", "open", "working", "partial"):
                grouped.setdefault(order["symbol"], []).append(deepcopy(order))
        self._runtime.open_orders = grouped
        quote = self._balances.get(self._quote)
        if quote:
            unrealized = sum(
                normalize_position(position)["unrealized_pnl"]
                for position in self._positions.values()
            )
            equity = float(quote.get("wallet_balance") or 0) + unrealized
            if equity > 0:
                self._runtime.update_equity(equity)
