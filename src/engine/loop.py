"""主循环调度：节流 → 特征 → LLM → 风控 → 执行 → 落库 → 告警。

本模块是「调度层」，自身不实现风控/执行细节，只负责按 SPEC 的下单前流水线
把各模块串起来，并处理：
- 全局熔断（日亏/回撤）最高优先级检查
- 5 分钟周期按 wall-clock 对齐（扣除本周期耗时）
- kill-switch：撤单 + 平仓 + 停机

"""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from src.config.schema import Credentials, ExecutionMode, Settings
from src.exchange.client import ExchangeClient
from src.exchange.filters import round_price
from src.exchange.market_data import MarketData
from src.exchange.orders import normalize_condition_order
from src.exchange.positions import normalize_position, normalize_symbol
from src.execution.executor import Executor, realized_pnl
from src.features.builder import build_context, build_position_snapshot
from src.llm.client import LLMClient
from src.llm.schema import Action, MarketContext, TradeDecision
from src.notify.telegram import Event, Notifier
from src.risk.manager import RejectCode, RiskContext, Verdict, validate
from src.state.runtime import RuntimeState
from src.store.repo import Store
from src.throttle.gate import should_call_llm


_RECONCILE_ACTIVE_INTERVAL_SECONDS = 15.0
_RECONCILE_IDLE_INTERVAL_SECONDS = 30.0
_COMMAND_POLL_INTERVAL_SECONDS = 1.0


class TradingEngine:
    def __init__(self, settings: Settings, creds: Credentials):
        self._settings = settings
        self._creds = creds
        self._client = ExchangeClient(settings, creds)
        self._market = MarketData(self._client, settings)
        self._executor = Executor(self._client, settings)
        self._llm = LLMClient(settings.llm, creds.anthropic_api_key)
        self._store = Store(settings.storage.db_path)
        self._notifier = Notifier(
            settings.notify, creds.telegram_bot_token, creds.telegram_chat_id
        )
        self._symbol_enabled = {symbol: True for symbol in settings.symbols}
        self._symbols = list(settings.symbols)
        self._symbol_needs_review: set[str] = set()
        self.runtime = RuntimeState()
        self._stopped = asyncio.Event()
        self._state_sync_lock = asyncio.Lock()
        self._reconcile_task: asyncio.Task | None = None

    # ---------- 生命周期 ----------
    async def startup(self) -> None:
        await self._store.connect()
        await self._store.sync_config_symbols(self._settings.symbols)
        await self._apply_runtime_settings()
        await self._client.load_markets()
        for symbol in self._symbols:
            filters = await self._client.ensure_symbol(symbol)
            await self._store.update_symbol_filters(symbol, filters)
            self._market.ensure_symbol(symbol)
        await self._market.refresh_all(self._symbols)
        self.runtime.roll_day_if_needed()
        if self._settings.storage.reconcile_on_start:
            try:
                positions = await self._client.fetch_positions(self._symbols)
                open_orders = await self._fetch_open_orders_safe()
                await self._store.reconcile(
                    positions, self.runtime, open_orders, symbols=self._symbols
                )
            except Exception as e:
                logger.warning("startup reconcile failed: {}", e)
        # 启动即拉一次权益，确保第一个周期的风控/上限基于真实权益而非退回保证金
        try:
            bal = await self._client.fetch_balance()
            await self._record_balance_snapshot(bal)
        except Exception as e:
            logger.warning("startup equity fetch failed: {}", e)
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(), name="exchange-reconciler"
        )
        logger.info("engine started (mode={}, db={}, equity={:.2f})",
                    self._settings.mode.value, self._settings.storage.db_path,
                    self.runtime.current_equity)

    async def shutdown(self) -> None:
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
            self._reconcile_task = None
        await self._market.stop()
        await self._llm.close()
        await self._notifier.close()
        await self._store.close()
        await self._client.close()
        logger.info("engine shutdown complete")

    # ---------- 主循环 ----------
    async def run(self) -> None:
        await self.startup()
        try:
            while not self.runtime.kill_switch and not self._stopped.is_set():
                cycle_start = time.monotonic()
                try:
                    await self._run_cycle()
                except Exception as e:  # 单周期异常不杀进程
                    logger.exception("cycle error: {}", e)
                    await self._notifier.send(Event.ERROR, f"cycle error: {e}")
                await self._sleep_to_next_cycle(cycle_start)
        finally:
            await self.shutdown()

    async def _sleep_to_next_cycle(self, cycle_start: float) -> None:
        interval = self._settings.cycle.interval_seconds
        while not self.runtime.kill_switch and not self._stopped.is_set():
            elapsed = time.monotonic() - cycle_start
            remaining = max(0.0, interval - elapsed)
            if remaining <= 0:
                return
            timeout = min(_COMMAND_POLL_INTERVAL_SECONDS, remaining)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=timeout)
                return
            except asyncio.TimeoutError:
                pass
            commands = await self._process_commands()
            if self.runtime.kill_switch or self._stopped.is_set():
                return
            if self._commands_should_wake_strategy(commands):
                logger.info("wake strategy cycle due command")
                return

    # ---------- 单周期 ----------
    async def _run_cycle(self) -> None:
        self.runtime.roll_day_if_needed()
        await self._process_commands()
        if self.runtime.kill_switch or self._stopped.is_set():
            return
        symbols = self._tracked_symbols()
        await self._market.refresh_all(symbols)

        # 0. 全局熔断（最高优先级）：日亏 / 回撤
        if await self._check_circuit_breaker():
            await self._snapshot()
            return

        # 逐 symbol 处理
        for symbol in symbols:
            try:
                await self._process_symbol(symbol)
            except Exception as e:
                logger.exception("process {} failed: {}", symbol, e)
            await self._process_commands()
            if self.runtime.kill_switch or self._stopped.is_set():
                return
            if self.runtime.halt_new_entries:
                await self._snapshot()
                return

        # 周期收尾：余额/持仓快照
        await self._snapshot()

    async def _process_commands(self) -> list[dict[str, str]]:
        """消费 web 操作面板下发的控制命令（Q1 方案A：解耦命令队列）。

        web 进程只写 control_commands 表，绝不直接碰交易所；命令在这里由交易进程
        串行执行，避免与主循环状态打架。
        """
        try:
            commands = await self._store.fetch_pending_commands()
        except Exception as e:
            logger.warning("fetch commands failed: {}", e)
            return []
        executed: list[dict[str, str]] = []
        for cmd in commands:
            name = cmd["command"]
            arg = cmd.get("arg", "")
            try:
                result = await self._exec_command(name, arg)
                await self._store.mark_command(cmd["id"], "done", result)
                logger.info("command {} done: {}", name, result)
                executed.append({
                    "command": name,
                    "arg": arg,
                    "status": "done",
                    "result": result,
                })
            except Exception as e:
                await self._store.mark_command(cmd["id"], "failed", str(e))
                logger.error("command {} failed: {}", name, e)
                executed.append({
                    "command": name,
                    "arg": arg,
                    "status": "failed",
                    "result": str(e),
                })
        return executed

    def _commands_should_wake_strategy(self, commands: list[dict[str, str]]) -> bool:
        for cmd in commands:
            if cmd.get("status") != "done":
                continue
            name = cmd.get("command")
            if name in ("RESUME", "RESUME_ALL_SYMBOLS"):
                return True
            if name == "SET_SYMBOL_ENABLED" and not self.runtime.halt_new_entries:
                enabled = self._symbol_enabled_arg_value(cmd.get("arg", ""))
                if enabled is True:
                    return True
        return False

    def _tracked_symbols(self) -> list[str]:
        return list(self._symbols or self._settings.symbols)

    async def _reload_symbols_from_store(self) -> list[str]:
        rows = await self._store.list_symbols()
        symbols = [normalize_symbol(row["symbol"]) for row in rows if row.get("status") == "active"]
        if not symbols:
            symbols = list(self._settings.symbols)
        self._symbols = symbols
        self._symbol_needs_review = {
            normalize_symbol(row["symbol"]) for row in rows if row.get("needs_review")
        }
        self._symbol_enabled = {
            symbol: bool(row.get("enabled")) and not bool(row.get("needs_review"))
            for row in rows
            if row.get("status") == "active"
            for symbol in [normalize_symbol(row["symbol"])]
        }
        for symbol in symbols:
            self._symbol_enabled.setdefault(symbol, True)
            self._market.ensure_symbol(symbol)
        return symbols

    @staticmethod
    def _symbol_enabled_arg_value(arg: str) -> bool | None:
        raw = (arg or "").strip()
        if "=" not in raw:
            return None
        _symbol_raw, value_raw = raw.split("=", 1)
        return value_raw.strip().lower() in ("1", "true", "yes", "on")

    async def _exec_command(self, name: str, arg: str) -> str:
        """执行单条命令，返回结果描述。未知命令抛错。"""
        if name == "KILL_SWITCH":
            await self.kill("web kill-switch")
            return "kill switch executed (cancel+flatten+stop)"
        if name == "PAUSE":
            self.runtime.halt_new_entries = True
            await self._store.set_runtime_setting("strategy.paused", "true")
            await self._notifier.send(Event.CIRCUIT_BREAK, "paused via web (no new entries)")
            return "strategy paused (persisted)"
        if name == "RESUME":
            self.runtime.halt_new_entries = False
            await self._store.set_runtime_setting("strategy.paused", "false")
            return "strategy resumed (persisted)"
        if name == "RESUME_ALL_SYMBOLS":
            return await self._resume_all_symbols()
        if name == "SET_SYMBOL_ENABLED":
            return await self._set_symbol_enabled(arg)
        if name == "ADD_SYMBOL":
            return await self._add_symbol(arg)
        if name == "REVIEW_SYMBOL":
            return await self._review_symbol(arg)
        if name == "CANCEL_AND_FLATTEN":
            return await self._cancel_and_flatten("web")
        if name == "STOP_ENGINE":
            await self.stop("web stop-engine")
            return "trading engine stopped; positions/orders untouched"
        if name == "REPAIR_SL_TP":
            return await self._repair_sl_tp(arg)
        raise ValueError(f"unknown command: {name}")

    async def _apply_runtime_settings(self) -> None:
        """启动时加载持久化运行态；缺省时用 config.yaml 并写入 DB。"""
        await self._reload_symbols_from_store()
        raw_paused = await self._store.get_runtime_setting("strategy.paused")
        if raw_paused is None:
            await self._store.set_runtime_setting("strategy.paused", "false")
        else:
            paused = raw_paused.strip().lower() in ("1", "true", "yes", "on")
            self.runtime.halt_new_entries = paused
            logger.info("runtime setting applied: strategy.paused={}", paused)
        for symbol in self._tracked_symbols():
            key = self._symbol_enabled_key(symbol)
            raw_enabled = await self._store.get_runtime_setting(key)
            if raw_enabled is None:
                default = self._symbol_enabled.get(symbol, symbol in self._settings.symbols)
                await self._store.set_runtime_setting(key, str(default).lower())
                self._symbol_enabled[symbol] = default
                continue
            enabled = (
                raw_enabled.strip().lower() in ("1", "true", "yes", "on")
                and symbol not in self._symbol_needs_review
            )
            self._symbol_enabled[symbol] = enabled
            logger.info("runtime setting applied: {}={}", key, enabled)

    async def _set_symbol_enabled(self, arg: str) -> str:
        """持久化单个币种的策略启用状态。"""
        raw = (arg or "").strip()
        if "=" not in raw:
            raise ValueError("SET_SYMBOL_ENABLED requires SYMBOL=true|false")
        symbol_raw, value_raw = raw.split("=", 1)
        symbol = normalize_symbol(symbol_raw)
        record = await self._store.get_symbol(symbol)
        if record is None or record.get("status") != "active":
            raise ValueError(f"symbol not registered: {symbol}")
        enabled = value_raw.strip().lower() in ("1", "true", "yes", "on")
        if enabled and record.get("needs_review"):
            raise ValueError(f"{symbol} needs manual review before enable")
        self._symbol_enabled[symbol] = enabled
        await self._store.set_symbol_enabled(symbol, enabled)
        await self._reload_symbols_from_store()
        return f"{symbol} strategy enabled set to {enabled} (persisted)"

    async def _add_symbol(self, arg: str) -> str:
        """动态新增币种：交易所验证 + 当前状态同步 + 默认停用。"""
        symbol = normalize_symbol((arg or "").strip())
        if not symbol:
            raise ValueError("ADD_SYMBOL requires a symbol")
        if not symbol.endswith("USDT"):
            raise ValueError("only USDT-M symbols are supported")

        review = await self._inspect_symbol_for_registration(symbol)

        await self._store.upsert_symbol_from_exchange(
            symbol=symbol,
            filters=review["filters"],
            exchange_state=review["exchange_state"],
            source="web",
            enabled=False,
            sync_status=review["sync_status"],
            needs_review=review["needs_review"],
        )
        await self._persist_symbol_review_snapshots(symbol, review)
        await self._refresh_symbol_after_review(symbol)
        await self._reload_symbols_from_store()

        if review["needs_review"]:
            return f"{symbol} added disabled; needs review: {self._review_problem_summary(review)}"
        return f"{symbol} added disabled; exchange confirmed flat"

    async def _review_symbol(self, arg: str) -> str:
        """人工复核动态币种：重新检查交易所，干净时解除 needs_review，但仍保持停用。"""
        symbol = normalize_symbol((arg or "").strip())
        if not symbol:
            raise ValueError("REVIEW_SYMBOL requires a symbol")
        record = await self._store.get_symbol(symbol)
        if record is None or record.get("status") != "active":
            raise ValueError(f"symbol not registered: {symbol}")

        review = await self._inspect_symbol_for_registration(symbol)
        await self._store.upsert_symbol_from_exchange(
            symbol=symbol,
            filters=review["filters"],
            exchange_state=review["exchange_state"],
            source=str(record.get("source") or "web"),
            enabled=False,
            sync_status=review["sync_status"],
            needs_review=review["needs_review"],
        )
        await self._persist_symbol_review_snapshots(symbol, review)
        await self._refresh_symbol_after_review(symbol)
        await self._reload_symbols_from_store()

        if review["needs_review"]:
            return f"{symbol} reviewed; still needs review: {self._review_problem_summary(review)}"
        return f"{symbol} reviewed; exchange confirmed flat; review cleared; remains disabled"

    async def _inspect_symbol_for_registration(self, symbol: str) -> dict:
        filters = await self._client.ensure_symbol(symbol)
        position_raw = await self._fetch_exchange_position_raw(symbol)
        position = normalize_position(position_raw) if position_raw else None
        open_orders = await self._client.fetch_open_orders(symbol)
        condition_orders = await self._client.fetch_open_condition_orders(symbol)

        has_position = bool(position and position.get("contracts", 0) > 0)
        has_open_orders = bool(open_orders)
        has_condition_orders = bool(condition_orders)
        needs_review = has_position or has_open_orders or has_condition_orders
        if has_position:
            sync_status = "live_position_found"
        elif has_open_orders:
            sync_status = "open_orders_found"
        elif has_condition_orders:
            sync_status = "condition_orders_found"
        else:
            sync_status = "confirmed_flat"
        return {
            "filters": filters,
            "position_raw": position_raw,
            "position": position,
            "open_orders": open_orders,
            "condition_orders": condition_orders,
            "exchange_state": {
                "position": position or {},
                "open_orders": open_orders,
                "condition_orders": condition_orders,
            },
            "has_position": has_position,
            "has_open_orders": has_open_orders,
            "has_condition_orders": has_condition_orders,
            "needs_review": needs_review,
            "sync_status": sync_status,
        }

    async def _persist_symbol_review_snapshots(self, symbol: str, review: dict) -> None:
        position_raw = review.get("position_raw")
        open_orders = list(review.get("open_orders") or [])
        condition_orders = list(review.get("condition_orders") or [])
        await self._store.snapshot_positions(
            [position_raw] if position_raw else [],
            symbols=[symbol],
        )
        if open_orders or condition_orders:
            await self._store.snapshot_open_orders([*open_orders, *condition_orders])

    async def _refresh_symbol_after_review(self, symbol: str) -> None:
        self._market.ensure_symbol(symbol)
        try:
            await self._market.refresh(symbol)
        except Exception as e:
            logger.warning("refresh dynamic symbol {} after review failed: {}", symbol, e)

    @staticmethod
    def _review_problem_summary(review: dict) -> str:
        parts = []
        if review.get("has_position"):
            parts.append("live position")
        if review.get("has_open_orders"):
            parts.append(f"{len(review.get('open_orders') or [])} open orders")
        if review.get("has_condition_orders"):
            parts.append(f"{len(review.get('condition_orders') or [])} condition orders")
        return ", ".join(parts)

    async def _resume_all_symbols(self) -> str:
        """Resume strategy and enable every configured symbol after a strict live precheck."""
        await self._assert_exchange_clear_for_resume_all()
        rows = await self._store.list_symbols()
        eligible = [
            normalize_symbol(row["symbol"])
            for row in rows
            if row.get("status") == "active" and not row.get("needs_review")
        ]
        values = {"strategy.paused": "false"}
        values.update({
            self._symbol_enabled_key(symbol): "true"
            for symbol in eligible
        })
        await self._store.set_runtime_settings(values)
        for symbol in eligible:
            await self._store.set_symbol_enabled(symbol, True)
        self.runtime.halt_new_entries = False
        for symbol in eligible:
            self._symbol_enabled[symbol] = True
        await self._reload_symbols_from_store()
        symbols = ", ".join(eligible)
        return (
            f"strategy resumed; enabled all symbols: {symbols}; "
            "precheck passed (no live positions/open orders/condition orders)"
        )

    async def _assert_exchange_clear_for_resume_all(self) -> None:
        """Block bulk resume unless exchange has no positions and no live orders."""
        symbols = self._tracked_symbols()
        positions = await self._client.fetch_positions(symbols)
        position_labels = []
        for raw in positions:
            pos = normalize_position(raw)
            if pos["contracts"] > 0:
                position_labels.append(self._position_label(pos))

        open_order_labels = []
        condition_order_labels = []
        for symbol in symbols:
            open_orders = await self._client.fetch_open_orders(symbol)
            open_order_labels.extend(
                self._open_order_label(symbol, order) for order in open_orders
            )
            condition_orders = await self._client.fetch_open_condition_orders(symbol)
            for raw in condition_orders:
                order = normalize_condition_order(raw)
                if not order["symbol"]:
                    order["symbol"] = symbol
                condition_order_labels.append(self._condition_order_label(order))

        problems = []
        if position_labels:
            problems.append("持仓: " + ", ".join(position_labels))
        if open_order_labels:
            problems.append("普通挂单: " + ", ".join(open_order_labels))
        if condition_order_labels:
            problems.append("条件单: " + ", ".join(condition_order_labels))
        if problems:
            raise ValueError("开启全部币种前检查失败，交易所仍有" + "；".join(problems))

    @staticmethod
    def _symbol_enabled_key(symbol: str) -> str:
        return f"symbol.enabled.{normalize_symbol(symbol)}"

    async def _cancel_and_flatten(self, source: str) -> str:
        """撤销挂单并平掉当前持仓，但不停止交易引擎。"""
        self.runtime.halt_new_entries = True
        await self._store.set_runtime_setting("strategy.paused", "true")
        symbols = self._tracked_symbols()
        await self._executor.cancel_all_orders(symbols=symbols)
        results = await self._executor.flatten_all(symbols=symbols)
        closed = 0
        for result in results:
            await self._store.log_order(result)
            if not result.get("filled"):
                continue
            closed += 1
            pnl = realized_pnl(
                side=result.get("pos_side", ""),
                entry_price=result.get("entry_price", 0.0),
                exit_price=result.get("price", 0.0),
                qty=result.get("qty", 0.0),
            )
            self.runtime.add_realized_pnl(pnl)
            self.runtime.positions.pop(result.get("symbol", ""), None)
            self.runtime.mark_order_event(result.get("symbol", ""))
        await self._notifier.send(
            Event.CIRCUIT_BREAK,
            f"cancel+flatten via {source}: closed={closed}, strategy paused",
        )
        return f"open orders canceled; flattened {closed} positions; strategy paused"

    async def _repair_sl_tp(self, arg: str) -> str:
        """补挂当前持仓缺失的 SL/TP 条件单。

        Web 只入队命令；真实交易所操作必须在 engine 中串行执行。这里每次都重新
        查询交易所持仓和未完成条件单，避免根据过期页面数据重复补单。
        """
        symbol = normalize_symbol((arg or "").strip())
        if not symbol:
            raise ValueError("REPAIR_SL_TP requires a symbol")
        if symbol not in self._tracked_symbols():
            raise ValueError(f"symbol not registered: {symbol}")

        position = await self._fetch_exchange_position(symbol)
        if position is None:
            return f"{symbol}: 交易所当前无持仓，不需要补保护单"

        side = position["side"]
        qty = float(position["contracts"] or 0.0)
        entry = float(position["entry_price"] or 0.0)
        mark = await self._current_mark_price(symbol, position)
        position["mark_price"] = mark
        if side not in ("long", "short") or qty <= 0 or entry <= 0 or mark <= 0:
            raise ValueError(
                f"{symbol}: 持仓数据不完整，无法做补单风控 "
                f"(side={side}, qty={qty}, entry={entry}, mark={mark})"
            )

        close_side = "sell" if side == "long" else "buy"
        active_orders = await self._active_protection_orders(symbol)
        stale_orders = self._stale_protection_orders(
            active_orders,
            side=side,
            close_side=close_side,
            qty=qty,
            entry=entry,
            mark=mark,
        )
        if stale_orders:
            remaining = await self._cancel_stale_condition_orders(
                symbol=symbol,
                orders=[order for order, _reason in stale_orders],
                reason="repair_sl_tp",
            )
            if remaining:
                details = ", ".join(
                    self._condition_order_label(order) for order in remaining
                )
                await self._disable_symbol_due_stale_conditions(symbol, details)
                raise ValueError(f"{symbol}: 存在无法撤销的陈旧条件单，已禁用该标的新开仓: {details}")
            active_orders = await self._active_protection_orders(symbol)

        missing = [
            kind for kind in ("SL", "TP")
            if not self._has_active_protection(
                active_orders,
                kind=kind,
                close_side=close_side,
                side=side,
                qty=qty,
                entry=entry,
                mark=mark,
            )
        ]
        if not missing:
            return f"{symbol}: SL/TP 条件单均已在交易所挂出"

        templates = await self._store.latest_protection_templates(symbol)
        latest_decision = await self._store.latest_open_decision(symbol)
        equity = await self._current_equity()

        specs: list[tuple[str, str, float]] = []
        accepted: list[str] = []
        rejected: list[str] = []
        filters = self._client.filters(symbol)
        for kind in missing:
            trigger, source = self._desired_protection_trigger(
                symbol=symbol,
                side=side,
                entry=entry,
                kind=kind,
                template=templates.get(kind),
                latest_decision=latest_decision,
            )
            if trigger <= 0:
                rejected.append(f"{kind}: 缺少历史触发价模板")
                continue
            trigger = float(round_price(trigger, filters))
            reason = self._validate_repair_trigger(
                symbol=symbol,
                side=side,
                kind=kind,
                trigger=trigger,
                entry=entry,
                mark=mark,
                qty=qty,
                equity=equity,
            )
            if reason:
                rejected.append(f"{kind}@{trigger:.2f}: {reason}")
                continue
            otype = "STOP_MARKET" if kind == "SL" else "TAKE_PROFIT_MARKET"
            specs.append((kind, otype, trigger))
            accepted.append(f"{kind}@{trigger:.2f}({source})")

        if not specs:
            raise ValueError(f"{symbol}: 未补挂保护单；" + "；".join(rejected))

        results = await self._executor.place_protection_orders(
            symbol=symbol,
            pos_side=side,
            qty=qty,
            specs=specs,
        )
        for order in results:
            await self._store.log_order(order)

        placed = [
            f"{o['kind']}@{float(o.get('price') or 0.0):.2f}"
            for o in results
            if o.get("status") == "placed"
        ]
        failed = [
            f"{o.get('kind')}:{(o.get('raw') or {}).get('error') or o.get('status')}"
            for o in results
            if o.get("status") != "placed"
        ]
        self.runtime.mark_order_event(symbol)

        if not placed:
            raise ValueError(f"{symbol}: 补单下发失败；" + "；".join(failed))

        parts = [f"{symbol}: 已补挂 {', '.join(placed)}"]
        if rejected:
            parts.append("未补: " + "；".join(rejected))
        if failed:
            parts.append("下发失败: " + "；".join(failed))
        logger.warning(
            "repair SL/TP {} side={} qty={} entry={} mark={} accepted={} rejected={} failed={}",
            symbol, side, qty, entry, mark, accepted, rejected, failed,
        )
        return "；".join(parts)

    async def _fetch_exchange_position_raw(self, symbol: str) -> dict | None:
        positions = await self._client.fetch_positions([symbol])
        for raw in positions:
            pos = normalize_position(raw)
            if pos["symbol"] == symbol and pos["contracts"] > 0:
                return raw
        return None

    async def _fetch_exchange_position(self, symbol: str) -> dict | None:
        raw = await self._fetch_exchange_position_raw(symbol)
        if raw is not None:
            return normalize_position(raw)
        return None

    async def _handle_missing_stop_after_open(
        self,
        *,
        decision: TradeDecision,
        open_result: dict,
        protection_orders: list[dict],
    ) -> None:
        if decision.stop_loss_pct <= 0:
            return
        has_stop = any(
            order.get("kind") == "SL"
            and order.get("status") == "placed"
            for order in protection_orders
        )
        if has_stop:
            return

        symbol = decision.symbol
        await self._disable_symbol_due_protection_failure(
            symbol, "SL protection was not confirmed after open"
        )
        await self._emergency_close_unprotected_position(
            symbol,
            reason="missing SL protection after open",
        )

    async def _disable_symbol_due_protection_failure(self, symbol: str, reason: str) -> None:
        self._symbol_enabled[symbol] = False
        await self._store.set_runtime_setting(self._symbol_enabled_key(symbol), "false")
        logger.error("disabled {} due protection failure: {}", symbol, reason)
        await self._notifier.send(Event.ERROR, f"{symbol} disabled: {reason}")

    async def _emergency_close_unprotected_position(self, symbol: str, *, reason: str) -> None:
        try:
            raw = await self._fetch_exchange_position_raw(symbol)
        except Exception as e:
            logger.error("fetch {} position for emergency close failed: {}", symbol, e)
            await self._notifier.send(Event.ERROR, f"{symbol} emergency close lookup failed: {e}")
            return
        if raw is None:
            logger.warning("{} emergency close skipped, no live position ({})", symbol, reason)
            return
        result = await self._executor.close_position(raw, mode=ExecutionMode.MARKET_TAKER)
        await self._store.log_order(result)
        if not result.get("filled"):
            logger.error("{} emergency close failed: {}", symbol, result)
            await self._notifier.send(Event.ERROR, f"{symbol} emergency close failed: {result}")
            return
        pnl = realized_pnl(
            side=result.get("pos_side", ""),
            entry_price=result.get("entry_price", 0.0),
            exit_price=result.get("price", 0.0),
            qty=result.get("qty", 0.0),
        )
        self.runtime.add_realized_pnl(pnl)
        self.runtime.positions.pop(symbol, None)
        self.runtime.mark_order_event(symbol)
        logger.error(
            "{} emergency closed unprotected position pnl={:.2f} reason={}",
            symbol, pnl, reason,
        )
        await self._notifier.send(
            Event.CLOSE,
            f"{symbol} emergency closed unprotected position pnl={pnl:.2f}",
        )

    async def _stale_condition_open_block_reason(self, decision: TradeDecision) -> str:
        symbol = decision.symbol
        try:
            active_orders = await self._active_protection_orders(symbol)
        except Exception as e:
            return f"{symbol}: 无法检查交易所条件单，拒绝新开仓: {e}"
        if not active_orders:
            return ""

        position = await self._fetch_exchange_position(symbol)
        if position is None:
            details = ", ".join(self._condition_order_label(order) for order in active_orders)
            await self._disable_symbol_due_stale_conditions(symbol, details)
            return f"{symbol}: 交易所存在无持仓条件单，已禁用该标的新开仓: {details}"

        side = position["side"]
        qty = float(position["contracts"] or 0.0)
        entry = float(position["entry_price"] or 0.0)
        mark = await self._current_mark_price(symbol, position)
        close_side = "sell" if side == "long" else "buy"
        stale = self._stale_protection_orders(
            active_orders,
            side=side,
            close_side=close_side,
            qty=qty,
            entry=entry,
            mark=mark,
        )
        if stale:
            details = ", ".join(
                f"{self._condition_order_label(order)} ({reason})"
                for order, reason in stale
            )
            await self._disable_symbol_due_stale_conditions(symbol, details)
            return f"{symbol}: 存在不匹配当前持仓的条件单，已禁用该标的新开仓: {details}"

        return f"{symbol}: 当前持仓已有保护条件单，暂不支持叠加开仓以避免保护数量失配"

    async def _disable_symbol_due_stale_conditions(self, symbol: str, details: str) -> None:
        self._symbol_enabled[symbol] = False
        await self._store.set_runtime_setting(self._symbol_enabled_key(symbol), "false")
        logger.error("disabled {} due stale condition orders: {}", symbol, details)
        await self._notifier.send(
            Event.ERROR,
            f"{symbol} disabled: stale condition orders remain: {details[:180]}",
        )

    async def _cancel_stale_condition_orders(
        self,
        *,
        symbol: str,
        orders: list[dict],
        reason: str,
    ) -> list[dict]:
        target_ids = {str(order.get("id") or "") for order in orders if order.get("id")}
        if not target_ids:
            return []
        for order in orders:
            order_id = str(order.get("id") or "")
            if not order_id:
                continue
            try:
                await self._client.cancel_condition_order(
                    symbol,
                    order_id,
                    client_algo_id=str(order.get("client_algo_id") or ""),
                )
                logger.info("cancel condition order {} {} via {}", symbol, order_id, reason)
            except Exception as e:
                logger.warning(
                    "cancel condition order {} {} via {} failed: {}",
                    symbol, order_id, reason, e,
                )
        await asyncio.sleep(0.5)
        after = await self._active_protection_orders(symbol)
        remaining = [order for order in after if str(order.get("id") or "") in target_ids]
        remaining_ids = {str(order.get("id") or "") for order in remaining}
        canceled_ids = target_ids - remaining_ids
        if canceled_ids:
            await self._store.mark_orders_status_by_exchange_ids(canceled_ids, "canceled")
        if remaining_ids:
            await self._store.mark_orders_status_by_exchange_ids(remaining_ids, "placed")
        return remaining

    async def _cancel_symbol_condition_orders(self, symbol: str, *, reason: str) -> list[dict]:
        before = await self._active_protection_orders(symbol)
        before_ids = {str(order.get("id") or "") for order in before if order.get("id")}
        if not before_ids:
            return []
        try:
            await self._client.cancel_all_condition_orders(symbol)
            logger.info("cancel all condition orders {} via {}", symbol, reason)
        except Exception as e:
            logger.warning("cancel all condition orders {} via {} failed: {}", symbol, reason, e)
        await asyncio.sleep(0.5)
        after = await self._active_protection_orders(symbol)
        remaining_ids = {str(order.get("id") or "") for order in after if order.get("id")}
        canceled_ids = before_ids - remaining_ids
        if canceled_ids:
            await self._store.mark_orders_status_by_exchange_ids(canceled_ids, "canceled")
        if remaining_ids:
            await self._store.mark_orders_status_by_exchange_ids(remaining_ids, "placed")
        return [order for order in after if str(order.get("id") or "") in before_ids]

    async def _active_protection_orders(self, symbol: str) -> list[dict]:
        orders = await self._client.fetch_open_condition_orders(symbol)
        out: list[dict] = []
        for raw in orders:
            order = normalize_condition_order(raw)
            if order["symbol"] == symbol and order["kind"] in ("SL", "TP"):
                order["status"] = "placed"
                out.append(order)
        return out

    def _has_active_protection(
        self,
        orders: list[dict],
        *,
        kind: str,
        close_side: str,
        side: str,
        qty: float,
        entry: float,
        mark: float,
    ) -> bool:
        for order in orders:
            if not self._protection_mismatch_reason(
                order,
                kind=kind,
                close_side=close_side,
                pos_side=side,
                qty=qty,
                entry=entry,
                mark=mark,
            ):
                return True
        return False

    def _stale_protection_orders(
        self,
        orders: list[dict],
        *,
        side: str,
        close_side: str,
        qty: float,
        entry: float,
        mark: float,
    ) -> list[tuple[dict, str]]:
        stale: list[tuple[dict, str]] = []
        for order in orders:
            kind = str(order.get("kind") or "")
            reason = self._protection_mismatch_reason(
                order,
                kind=kind,
                close_side=close_side,
                pos_side=side,
                qty=qty,
                entry=entry,
                mark=mark,
            )
            if reason:
                stale.append((order, reason))
        return stale

    @staticmethod
    def _safe_float(value: object) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _position_label(position: dict) -> str:
        return (
            f"{position.get('symbol')}:{position.get('side')} "
            f"qty={float(position.get('contracts') or 0.0):g} "
            f"entry={float(position.get('entry_price') or 0.0):g}"
        )

    @classmethod
    def _open_order_label(cls, symbol: str, order: dict) -> str:
        info = order.get("info") if isinstance(order.get("info"), dict) else {}
        raw_symbol = normalize_symbol(order.get("symbol") or info.get("symbol") or symbol)
        order_id = str(order.get("id") or info.get("orderId") or "")
        order_type = str(order.get("type") or info.get("type") or "")
        side = str(order.get("side") or info.get("side") or "").lower()
        qty = cls._safe_float(
            order.get("amount")
            or order.get("remaining")
            or info.get("origQty")
            or info.get("quantity")
        )
        price = cls._safe_float(order.get("price") or info.get("price"))
        return f"{raw_symbol}:{order_type}#{order_id} side={side} qty={qty:g} price={price:g}"

    @staticmethod
    def _condition_order_label(order: dict) -> str:
        kind = order.get("kind") or order.get("order_type") or "CONDITION"
        return (
            f"{order.get('symbol')}:{kind}#{order.get('id')} "
            f"qty={float(order.get('qty') or 0.0):g} "
            f"trigger={float(order.get('trigger_price') or 0.0):g}"
        )

    @staticmethod
    def _protection_mismatch_reason(
        order: dict,
        *,
        kind: str,
        close_side: str,
        pos_side: str,
        qty: float,
        entry: float,
        mark: float,
    ) -> str:
        if order.get("kind") != kind:
            return "kind mismatch"
        if order.get("status") != "placed":
            return "not placed"
        if not order.get("reduce_only"):
            return "not reduceOnly"
        side = (order.get("side") or "").lower()
        if side and side != close_side:
            return f"side {side} != close side {close_side}"
        order_qty = float(order.get("qty") or 0.0)
        qty_tol = max(abs(qty) * 1e-6, 1e-12)
        if order_qty <= 0 or abs(order_qty - qty) > qty_tol:
            return f"qty {order_qty:g} != position qty {qty:g}"
        trigger = float(order.get("trigger_price") or 0.0)
        if trigger <= 0 or entry <= 0 or mark <= 0:
            return "invalid trigger/entry/mark"
        if pos_side == "long":
            if kind == "SL" and not (trigger < mark and trigger < entry):
                return f"long SL trigger {trigger:g} not below mark/entry"
            if kind == "TP" and not (trigger > mark and trigger > entry):
                return f"long TP trigger {trigger:g} not above mark/entry"
        elif pos_side == "short":
            if kind == "SL" and not (trigger > mark and trigger > entry):
                return f"short SL trigger {trigger:g} not above mark/entry"
            if kind == "TP" and not (trigger < mark and trigger < entry):
                return f"short TP trigger {trigger:g} not below mark/entry"
        else:
            return f"unknown position side {pos_side}"
        return ""

    async def _current_mark_price(self, symbol: str, position: dict) -> float:
        mark = float(position.get("mark_price") or 0.0)
        if mark > 0:
            return mark
        ticker = await self._client.fetch_ticker(symbol)
        raw_mark = (
            ticker.get("mark")
            or (ticker.get("info") or {}).get("markPrice")
            or ticker.get("last")
        )
        return float(raw_mark or 0.0)

    async def _current_equity(self) -> float:
        try:
            balance = await self._client.fetch_balance()
            total = (balance.get("total") or {}).get(self._settings.account.quote_asset)
            equity = float(total or 0.0)
            if equity > 0:
                self.runtime.update_equity(equity)
                return equity
        except Exception as e:
            logger.warning("fetch equity for repair failed: {}", e)
        if self.runtime.current_equity > 0:
            return self.runtime.current_equity
        return float(self._settings.account.initial_capital)

    def _desired_protection_trigger(
        self,
        *,
        symbol: str,
        side: str,
        entry: float,
        kind: str,
        template: dict | None,
        latest_decision: dict | None,
    ) -> tuple[float, str]:
        if template and float(template.get("price") or 0.0) > 0:
            return float(template["price"]), "历史条件单"

        action = (latest_decision or {}).get("action", "")
        expected_action = "OPEN_LONG" if side == "long" else "OPEN_SHORT"
        if action != expected_action:
            return 0.0, ""
        pct_key = "stop_loss_pct" if kind == "SL" else "take_profit_pct"
        pct = float((latest_decision or {}).get(pct_key) or 0.0)
        if pct <= 0:
            return 0.0, ""
        if kind == "SL":
            trigger = entry * (1 - pct) if side == "long" else entry * (1 + pct)
        else:
            trigger = entry * (1 + pct) if side == "long" else entry * (1 - pct)
        logger.info(
            "repair {} {} trigger reconstructed from decision {} pct={}",
            symbol, kind, (latest_decision or {}).get("id"), pct,
        )
        return trigger, "最近开仓决策"

    def _validate_repair_trigger(
        self,
        *,
        symbol: str,
        side: str,
        kind: str,
        trigger: float,
        entry: float,
        mark: float,
        qty: float,
        equity: float,
    ) -> str:
        if trigger <= 0 or entry <= 0 or mark <= 0 or qty <= 0:
            return "价格或数量无效"
        if side == "long":
            if kind == "SL" and not (trigger < mark and trigger < entry):
                return f"多单止损必须低于当前标记价 {mark:.2f} 且低于开仓价 {entry:.2f}"
            if kind == "TP" and not (trigger > mark and trigger > entry):
                return f"多单止盈必须高于当前标记价 {mark:.2f} 且高于开仓价 {entry:.2f}"
        elif side == "short":
            if kind == "SL" and not (trigger > mark and trigger > entry):
                return f"空单止损必须高于当前标记价 {mark:.2f} 且高于开仓价 {entry:.2f}"
            if kind == "TP" and not (trigger < mark and trigger < entry):
                return f"空单止盈必须低于当前标记价 {mark:.2f} 且低于开仓价 {entry:.2f}"
        else:
            return f"未知持仓方向 {side}"

        if kind == "SL":
            loss = (entry - trigger) * qty if side == "long" else (trigger - entry) * qty
            max_loss = equity * (self._settings.risk.max_loss_per_trade_pct / 100.0)
            if equity <= 0 or max_loss <= 0:
                return "无法获取账户权益，不能校验止损风险"
            if loss < 0:
                return "止损触发价方向错误"
            if loss > max_loss:
                return (
                    f"理论止损亏损 {loss:.2f} USDT 超过上限 {max_loss:.2f} USDT "
                    f"({self._settings.risk.max_loss_per_trade_pct}% of {equity:.2f})"
                )
        logger.debug(
            "repair trigger valid {} {} side={} trigger={} entry={} mark={} qty={}",
            symbol, kind, side, trigger, entry, mark, qty,
        )
        return ""

    async def _check_circuit_breaker(self) -> bool:
        """日亏或回撤超限 → 平仓 + 停开新仓 + 告警。返回是否已熔断。"""
        risk = self._settings.risk
        rt = self.runtime
        breached = None
        # 日亏限额 = 权益 × daily_max_loss_pct（随资金缩放）；权益未知时退回 0 不触发
        base = rt.current_equity if rt.current_equity > 0 else rt.equity_peak
        daily_max_loss = base * (risk.daily_max_loss_pct / 100.0)
        if daily_max_loss > 0 and rt.day_realized_pnl <= -abs(daily_max_loss):
            breached = (f"daily loss {rt.day_realized_pnl:.2f} <= -{daily_max_loss:.2f} "
                        f"({risk.daily_max_loss_pct}% of {base:.2f})")
        elif rt.drawdown_pct >= risk.max_drawdown_pct:
            breached = f"drawdown {rt.drawdown_pct:.2f}% >= {risk.max_drawdown_pct}%"
        if breached and not rt.halt_new_entries:
            logger.warning("CIRCUIT BREAKER: {}", breached)
            rt.trip_breaker()
            try:
                await self._executor.flatten_all(symbols=self._tracked_symbols())
            except Exception as e:
                logger.error("circuit-breaker flatten failed: {}", e)
            await self._notifier.send(Event.CIRCUIT_BREAK, breached)
            return True
        return rt.halt_new_entries

    async def _process_symbol(self, symbol: str) -> None:
        snap = self._market.snapshot(symbol)
        if not self._symbol_enabled.get(symbol, True):
            logger.info("[skip-llm] {} reason=symbol disabled", symbol)
            await self._store.log_decision(
                symbol=symbol, skipped=True, skip_reason="symbol disabled",
                ref_price=snap.last_price,
            )
            return
        position = build_position_snapshot(self.runtime.positions.get(symbol))

        # 1. 节流：是否调用 LLM
        gate = should_call_llm(
            symbol=symbol,
            last_price=snap.last_price,
            last_decision_px=self.runtime.last_decision_price.get(symbol),
            position=position,
            price_change_pct=self._settings.throttle.price_change_pct,
            pnl_alert_pct=self._settings.throttle.pnl_alert_pct,
            order_event=self.runtime.pop_order_event(symbol),
            trigger_on_order_event=self._settings.throttle.trigger_on_order_event,
            skip_count=self.runtime.skip_count.get(symbol, 0),
            max_skip_cycles=self._settings.throttle.max_skip_cycles,
        )
        if not gate.trigger:
            self.runtime.record_skip(symbol)
            if self._settings.cycle.heartbeat_on_skip:
                logger.info("[skip-llm] {} reason={}", symbol, gate.reason)
            await self._store.log_decision(
                symbol=symbol, skipped=True, skip_reason=gate.reason, ref_price=snap.last_price
            )
            return

        # 2. 特征 → LLM 决策（失败降级 HOLD 由 LLMClient 内部保证）
        margin = await self._fetch_margin_safe()
        higher_tf = await self._fetch_higher_tf_safe(symbol)
        ctx = build_context(
            symbol=symbol, snapshot=snap, position=position,
            available_margin=margin, settings=self._settings,
            equity=self.runtime.current_equity,
            higher_tf_klines=higher_tf,
        )
        if ctx is None:
            logger.warning("context unavailable for {}, skip", symbol)
            return

        decision = await self._llm.decide(ctx)
        self.runtime.record_decision(symbol, snap.last_price)
        await self._store.log_decision(
            symbol=symbol, decision=decision, ctx=ctx, skipped=False, ref_price=snap.last_price
        )

        # CLOSE 优先处理（不受开仓限额约束）
        if decision.action == Action.CLOSE:
            await self._handle_close(symbol)
            return
        if decision.action == Action.HOLD:
            return

        # 3. 风控逐项校验
        await self._handle_open(decision, ctx)

    async def _handle_open(self, decision: TradeDecision, ctx: MarketContext) -> None:
        symbol = decision.symbol
        sym_margin = self._position_margin(symbol)
        total_margin = sum(self._position_margin(s) for s in self._tracked_symbols())
        rctx = RiskContext(
            last_price=ctx.last_price,
            available_margin=ctx.available_margin,
            equity=self.runtime.current_equity,
            symbol_position_margin=sym_margin,
            total_open_margin=total_margin,
            day_realized_pnl=self.runtime.day_realized_pnl,
            drawdown_pct=self.runtime.drawdown_pct,
            halt_new_entries=self.runtime.halt_new_entries,
            kill_switch=self.runtime.kill_switch,
        )
        verdict = validate(decision, rctx, self._settings)
        if not verdict.passed:
            logger.warning("[reject] {} {}", symbol, verdict.reason)
            await self._store.log_reject(symbol=symbol, verdict=verdict, decision=decision)
            await self._notifier.send(Event.REJECT, f"{symbol} {verdict.reason}")
            return

        stale_reason = await self._stale_condition_open_block_reason(decision)
        if stale_reason:
            verdict = Verdict.reject(RejectCode.STALE_CONDITION_ORDER, stale_reason)
            logger.warning("[reject] {} {}", symbol, verdict.reason)
            await self._store.log_reject(symbol=symbol, verdict=verdict, decision=decision)
            await self._notifier.send(Event.REJECT, f"{symbol} {verdict.reason}")
            return

        # 4. 执行（精度规整在 executor 内）
        result = await self._executor.open_position(
            decision=decision, qty=verdict.qty, price=ctx.last_price
        )
        logged = await self._store.log_order(result)
        if result["status"] == "rejected":
            await self._notifier.send(Event.REJECT, f"{symbol} below min order")
            return
        if not result["filled"]:
            await self._notifier.send(
                Event.REJECT,
                f"{symbol} open not filled status={result.get('status')} "
                f"mode={result.get('execution_mode') or 'unknown'}",
            )
            return
        if result["filled"]:
            self.runtime.mark_order_event(symbol)
            await self._notifier.send(
                Event.OPEN, f"{symbol} {decision.action.value} qty={result['qty']} "
                f"notional={result['notional']:.2f}"
            )
            if self._settings.execution.attach_sl_tp:
                sltp = await self._executor.place_sl_tp(
                    decision=decision, entry_price=result["price"], qty=result["qty"]
                )
                trade_id = int((logged or {}).get("trade_id") or 0)
                for o in sltp:
                    if trade_id > 0:
                        o["trade_id"] = trade_id
                    if decision.leverage > 0:
                        o["leverage"] = decision.leverage
                        o["margin"] = float(o.get("notional") or 0.0) / decision.leverage
                    await self._store.log_order(o)
                await self._handle_missing_stop_after_open(
                    decision=decision,
                    open_result=result,
                    protection_orders=sltp,
                )

    async def _handle_close(self, symbol: str) -> None:
        raw = self.runtime.positions.get(symbol)
        if not raw:
            logger.info("[{}] CLOSE requested but no position", symbol)
            return
        result = await self._executor.close_position(
            raw,
            mode=self._settings.execution.normal_exit_mode,
        )
        await self._store.log_order(result)
        if result["filled"]:
            # 计算本次平仓已实现盈亏并累加（驱动日亏熔断）
            pnl = realized_pnl(
                side=result.get("pos_side", ""),
                entry_price=result.get("entry_price", 0.0),
                exit_price=result["price"],
                qty=result["qty"],
            )
            self.runtime.add_realized_pnl(pnl)
            self.runtime.mark_order_event(symbol)
            if result.get("status") == "partial":
                try:
                    repair_result = await self._repair_sl_tp(symbol)
                    logger.warning("{} partial close protection repaired: {}", symbol, repair_result)
                except Exception as e:
                    await self._disable_symbol_due_protection_failure(
                        symbol, f"protection repair failed after partial close: {e}"
                    )
                    await self._emergency_close_unprotected_position(
                        symbol, reason="protection repair failed after partial close"
                    )
                await self._notifier.send(
                    Event.CLOSE,
                    f"{symbol} partial close qty={result['qty']} pnl={pnl:.2f}",
                )
                return
            # 已显式平仓：从运行态移除，避免 _snapshot 的差异检测重复计账
            self.runtime.positions.pop(symbol, None)
            remaining = await self._cancel_symbol_condition_orders(
                symbol, reason="explicit_close"
            )
            if remaining:
                details = ", ".join(
                    self._condition_order_label(order) for order in remaining
                )
                await self._disable_symbol_due_stale_conditions(symbol, details)
            await self._notifier.send(
                Event.CLOSE,
                f"{symbol} closed pnl={pnl:.2f} day_pnl={self.runtime.day_realized_pnl:.2f}",
            )

    # ---------- 辅助 ----------
    def _position_notional(self, symbol: str) -> float:
        p = self.runtime.positions.get(symbol)
        if not p:
            return 0.0
        contracts = abs(float(p.get("contracts") or 0))
        mark = float(p.get("markPrice") or p.get("entryPrice") or 0)
        return contracts * mark

    def _position_margin(self, symbol: str) -> float:
        """估算当前持仓占用保证金。

        交易所返回字段不完全稳定：优先使用直接保证金字段，缺失时用
        名义价值 / 杠杆 估算；杠杆缺失时按名义价值保守计入。
        """
        p = self.runtime.positions.get(symbol)
        if not p:
            return 0.0
        info = p.get("info") or {}
        for key in ("initialMargin", "positionInitialMargin", "isolatedMargin", "collateral"):
            val = p.get(key)
            if val is None:
                val = info.get(key)
            if val not in (None, ""):
                try:
                    return abs(float(val))
                except (TypeError, ValueError):
                    pass
        notional = self._position_notional(symbol)
        leverage = float(p.get("leverage") or info.get("leverage") or 0)
        if leverage > 0:
            return notional / leverage
        return notional

    async def _fetch_margin_safe(self) -> float:
        try:
            return await self._client.fetch_available_margin(self._settings.account.quote_asset)
        except Exception as e:
            logger.warning("fetch margin failed: {}", e)
            return 0.0

    async def _fetch_higher_tf_safe(self, symbol: str) -> dict[str, list]:
        """拉取配置的更高周期 K 线，供多周期共振。失败返回空(不阻塞主决策)。"""
        out: dict[str, list] = {}
        for tf in self._settings.llm.higher_timeframes:
            try:
                out[tf] = await self._client.fetch_ohlcv(symbol, tf, 60)
            except Exception as e:
                logger.warning("fetch higher tf {} {} failed: {}", tf, symbol, e)
        return out

    async def _fetch_positions_safe(self) -> list[dict]:
        try:
            return await self._client.fetch_positions(self._tracked_symbols())
        except Exception as e:
            logger.warning("fetch positions failed: {}", e)
            return []

    async def _fetch_open_orders_safe(self) -> list[dict]:
        """启动对账用：拉取普通未完成单和条件单，失败跳过单个 symbol。"""
        out: list[dict] = []
        for sym in self._tracked_symbols():
            try:
                out.extend(await self._client.fetch_open_orders(sym))
            except Exception as e:
                logger.warning("fetch open orders failed {}: {}", sym, e)
            try:
                out.extend(await self._client.fetch_open_condition_orders(sym))
            except Exception as e:
                logger.warning("fetch open condition orders failed {}: {}", sym, e)
        return out

    async def _reconcile_loop(self) -> None:
        """独立交易所对账循环。paused 时也运行，避免页面和 runtime 变成旧状态。"""
        while not self.runtime.kill_switch and not self._stopped.is_set():
            interval = (
                _RECONCILE_ACTIVE_INTERVAL_SECONDS
                if self.runtime.positions
                else _RECONCILE_IDLE_INTERVAL_SECONDS
            )
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
            try:
                await self._reconcile_exchange_state("periodic")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("periodic exchange reconcile failed: {}", e)

    async def _reconcile_exchange_state(self, reason: str) -> None:
        """同步交易所当前状态，并执行保护单不变量检查。"""
        logger.debug("exchange reconcile start: {}", reason)
        await self._snapshot()
        await self._sync_open_orders_snapshot()
        await self._enforce_exchange_invariants(reason)

    async def _sync_open_orders_snapshot(self) -> None:
        orders = await self._fetch_open_orders_safe()
        self.runtime.open_orders = {}
        for order in orders:
            sym = normalize_symbol(order.get("symbol"))
            self.runtime.open_orders.setdefault(sym, []).append(order)
        if orders:
            await self._store.snapshot_open_orders(orders)

    async def _enforce_exchange_invariants(self, reason: str) -> None:
        positions = {
            normalize_position(raw)["symbol"]: normalize_position(raw)
            for raw in await self._client.fetch_positions(self._tracked_symbols())
            if normalize_position(raw)["contracts"] > 0
        }
        for symbol in self._tracked_symbols():
            try:
                active_orders = await self._active_protection_orders(symbol)
            except Exception as e:
                logger.warning("reconcile {} active protection query failed: {}", symbol, e)
                continue
            position = positions.get(symbol)
            if position is None:
                live_ids = {str(order.get("id") or "") for order in active_orders}
                await self._store.mark_symbol_conditions_not_live(symbol, live_ids)
                if active_orders and self._symbol_enabled.get(symbol, True):
                    remaining = await self._cancel_stale_condition_orders(
                        symbol=symbol,
                        orders=active_orders,
                        reason=reason,
                    )
                    if remaining:
                        details = ", ".join(
                            self._condition_order_label(order) for order in remaining
                        )
                        await self._disable_symbol_due_stale_conditions(symbol, details)
                continue

            if not await self._should_enforce_position_protection(symbol, reason):
                continue

            side = position["side"]
            qty = float(position["contracts"] or 0.0)
            entry = float(position["entry_price"] or 0.0)
            mark = await self._current_mark_price(symbol, position)
            close_side = "sell" if side == "long" else "buy"
            stale = self._stale_protection_orders(
                active_orders,
                side=side,
                close_side=close_side,
                qty=qty,
                entry=entry,
                mark=mark,
            )
            if stale:
                remaining = await self._cancel_stale_condition_orders(
                    symbol=symbol,
                    orders=[order for order, _reason in stale],
                    reason=reason,
                )
                if remaining:
                    details = ", ".join(
                        self._condition_order_label(order) for order in remaining
                    )
                    await self._disable_symbol_due_stale_conditions(symbol, details)
                active_orders = await self._active_protection_orders(symbol)
            has_stop = self._has_active_protection(
                active_orders,
                kind="SL",
                close_side=close_side,
                side=side,
                qty=qty,
                entry=entry,
                mark=mark,
            )
            if not has_stop:
                await self._disable_symbol_due_protection_failure(
                    symbol,
                    f"SL protection missing during exchange reconcile ({reason})",
                )
                await self._emergency_close_unprotected_position(
                    symbol,
                    reason=f"missing SL during exchange reconcile ({reason})",
                )

    async def _should_enforce_position_protection(self, symbol: str, reason: str) -> bool:
        """Only auto-fix/close positions that are both enabled and locally managed."""
        if not self._symbol_enabled.get(symbol, False):
            logger.warning(
                "{} live position detected during {}, but symbol is disabled; "
                "skip auto protection enforcement",
                symbol, reason,
            )
            return False
        try:
            managed = await self._store.has_open_trade(symbol)
        except Exception as e:
            logger.warning(
                "{} live position detected during {}, but local trade ownership check failed: {}; "
                "skip auto protection enforcement",
                symbol, reason, e,
            )
            return False
        if managed:
            return True

        self._symbol_enabled[symbol] = False
        await self._store.set_symbol_enabled(symbol, False)
        message = (
            f"{symbol} live position detected during {reason}, but no local open trade "
            "exists; symbol disabled and auto close skipped"
        )
        logger.error(message)
        await self._notifier.send(Event.ERROR, message)
        return False

    async def _snapshot(self) -> None:
        """刷新持仓/余额快照，更新运行态权益与回撤。

        差异检测：上一周期存在、本周期消失的持仓，视为被 SL/TP 或交易所侧外部平仓，
        用「入场价 vs 最后已知标记价」估算其已实现盈亏并累加（驱动日亏熔断）。
        显式 CLOSE 已在 _handle_close 计账并从 runtime.positions 移除，不会重复。
        """
        async with self._state_sync_lock:
            await self._snapshot_unlocked()

    async def _snapshot_unlocked(self) -> None:
        prev_positions = dict(self.runtime.positions)
        positions = await self._fetch_positions_safe()
        new_positions = {
            (p.get("symbol") or "").replace("/USDT:USDT", "USDT"): p for p in positions
        }
        condition_exits = self._detect_external_closes(prev_positions, new_positions)
        self.runtime.positions = new_positions
        await self._store.snapshot_positions(positions, symbols=self._tracked_symbols())
        for exit_event in condition_exits:
            remaining = await self._cancel_symbol_condition_orders(
                exit_event["symbol"], reason="external_close"
            )
            await self._store.mark_condition_exit(**exit_event)
            if remaining:
                remaining_ids = {
                    str(order.get("id") or "") for order in remaining if order.get("id")
                }
                await self._store.mark_orders_status_by_exchange_ids(remaining_ids, "placed")
                details = ", ".join(self._condition_order_label(order) for order in remaining)
                await self._disable_symbol_due_stale_conditions(exit_event["symbol"], details)
        try:
            bal = await self._client.fetch_balance()
            await self._record_balance_snapshot(bal)
        except Exception as e:
            logger.warning("balance snapshot failed: {}", e)

    async def _record_balance_snapshot(self, balance: dict) -> None:
        total = (balance.get("total") or {}).get(self._settings.account.quote_asset) or 0.0
        free = (balance.get("free") or {}).get(self._settings.account.quote_asset) or 0.0
        total = float(total)
        self.runtime.update_equity(total)
        await self._store.snapshot_balance(
            total_equity=total,
            available_margin=float(free),
            runtime=self.runtime,
            quote_asset=self._settings.account.quote_asset,
        )

    def _detect_external_closes(self, prev: dict[str, dict], curr: dict[str, dict]) -> list[dict]:
        """对比前后持仓，对消失的持仓估算已实现盈亏并累加。

        估算用最后已知标记价作为出场价（SL/TP 实际触发价与之接近，未计手续费），
        是近似值；精确对账以交易所 income 流水为准（见 RUNBOOK 复盘章节）。
        """
        condition_exits: list[dict] = []
        for symbol, old in prev.items():
            still_open = float((curr.get(symbol) or {}).get("contracts") or 0) != 0
            if still_open:
                continue
            qty = abs(float(old.get("contracts") or 0))
            if qty == 0:
                continue
            entry = float(old.get("entryPrice") or 0)
            exit_px = float(old.get("markPrice") or entry)
            pnl = realized_pnl(side=(old.get("side") or ""), entry_price=entry,
                               exit_price=exit_px, qty=qty)
            self.runtime.add_realized_pnl(pnl)
            self.runtime.mark_order_event(symbol)
            kind = self._infer_condition_exit_kind(old, exit_px)
            if kind:
                condition_exits.append({
                    "symbol": symbol,
                    "triggered_kind": kind,
                    "qty": qty,
                    "price": exit_px,
                })
            logger.info("[{}] external close detected, est. pnl={:.2f} day_pnl={:.2f}",
                        symbol, pnl, self.runtime.day_realized_pnl)
        return condition_exits

    @staticmethod
    def _infer_condition_exit_kind(position: dict, exit_price: float) -> str:
        side = (position.get("side") or "").lower()
        entry = float(position.get("entryPrice") or 0)
        if entry <= 0 or exit_price <= 0:
            return ""
        if side == "long":
            return "TP" if exit_price >= entry else "SL"
        if side == "short":
            return "TP" if exit_price <= entry else "SL"
        return ""

    # <!-- APPEND_CYCLE -->

    # ---------- Stop / Kill switch ----------
    async def stop(self, reason: str = "manual") -> None:
        """停止交易引擎主循环，不撤单、不平仓。"""
        logger.warning("trading engine stop requested: {}", reason)
        self.runtime.halt_new_entries = True
        self._stopped.set()
        await self._notifier.send(Event.CIRCUIT_BREAK, f"engine stopped: {reason}")

    # ---------- Kill switch ----------
    async def kill(self, reason: str = "manual") -> None:
        """紧急停机：撤单 + 平仓 + 停止循环。"""
        logger.warning("KILL SWITCH triggered: {}", reason)
        self.runtime.trigger_kill()
        self._stopped.set()
        try:
            symbols = self._tracked_symbols()
            await self._executor.cancel_all_orders(symbols=symbols)
            await self._executor.flatten_all(symbols=symbols)
        except Exception as e:
            logger.error("kill switch flatten failed: {}", e)
        await self._notifier.send(Event.KILL_SWITCH, reason)
