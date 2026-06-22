"""主循环调度：节流 → 特征 → LLM → 风控 → 执行 → 落库 → 告警。

本模块是「调度层」，自身不实现风控/执行细节，只负责按 SPEC 的下单前流水线
把各模块串起来，并处理：
- 全局熔断（日亏/回撤）最高优先级检查
- 5 分钟周期按 wall-clock 对齐（扣除本周期耗时）
- kill-switch：撤单 + 平仓 + 停机

"""
from __future__ import annotations

import asyncio
import json
import time

from loguru import logger

from src.config.schema import Credentials, ExecutionMode, Settings
from src.engine.decision_guards import (
    CloseConfirmationState,
    SltpAdjustState,
    evaluate_close_guard,
    evaluate_sltp_adjust_guard,
)
from src.exchange.client import ExchangeClient
from src.exchange.events import rest_snapshot_event
from src.exchange.fills import ccxt_trade_fill, private_order_trade_fill
from src.exchange.filters import normalize_order, round_price
from src.exchange.market_data import MarketData
from src.exchange.orders import normalize_condition_order
from src.exchange.positions import normalize_position, normalize_symbol
from src.exchange.user_stream import BinanceUserDataStream
from src.execution.executor import Executor, ProtectionOrderSpec, realized_pnl
from src.execution.settings import (
    RUNTIME_EXECUTION_KEY,
    RUNTIME_EXECUTION_VERSION_KEY,
    decode_execution,
    encode_execution,
    execution_defaults_from_settings,
    execution_public,
    execution_runtime_to_config,
    validate_execution_payload,
)
from src.engine.settings import (
    RUNTIME_ENGINE_KEY,
    RUNTIME_ENGINE_VERSION_KEY,
    decode_engine,
    encode_engine,
    engine_defaults_from_settings,
    engine_public,
    validate_engine_payload,
)
from src.features.builder import build_context, build_position_snapshot
from src.llm.client import LLMClient
from src.llm.schema import (
    Action,
    MarketContext,
    PositionSnapshot,
    ProtectionOrderSnapshot,
    TradeDecision,
)
from src.notify.telegram import Event, Notifier
from src.risk.manager import RejectCode, RiskContext, Verdict, validate
from src.risk.settings import (
    RUNTIME_RISK_KEY,
    RUNTIME_RISK_VERSION_KEY,
    decode_risk,
    encode_risk,
    risk_public,
    validate_risk_payload,
)
from src.state.runtime import RuntimeState
from src.state.account import AccountStateCoordinator
from src.store.repo import Store
from src.throttle.feature_snapshot import FeatureSnapshot, build_feature_snapshot
from src.throttle.gate import should_call_llm


_COMMAND_POLL_INTERVAL_SECONDS = 1.0
_ENTRY_CLAIM_TTL_MS = 300_000
_POST_OPEN_POSITION_CONFIRM_DELAYS_SECONDS = (0.0, 0.3, 0.7, 1.2, 2.0, 3.0)
_EXCHANGE_FLAT_CONFIRM_DELAYS_SECONDS = (0.5, 1.5, 3.0, 5.0)
_EXCHANGE_FLAT_MIN_OPEN_AGE_MS = 60_000
_POST_CLOSE_RECONCILE_GRACE_SECONDS = 30.0
_UNMANAGED_LIVE_CONFIRM_DELAYS_SECONDS = (0.5, 1.5, 3.0)
_RISK_DAY_KEY = "risk.drawdown.day_key"
_RISK_DAY_EQUITY_PEAK_KEY = "risk.drawdown.day_equity_peak"
_RISK_DRAWDOWN_BYPASS_DAY_KEY = "risk.drawdown.bypass_day"
_RISK_DRAWDOWN_BYPASS_AT_MS_KEY = "risk.drawdown.bypass_at_ms"


class ProtectionRepairError(ValueError):
    def __init__(
        self,
        symbol: str,
        message: str,
        *,
        reason_code: str = "PROTECTION_REPAIR_FAILED",
    ):
        super().__init__(message)
        self.symbol = symbol
        self.reason_code = reason_code


class TradingEngine:
    def __init__(self, settings: Settings, creds: Credentials):
        self._settings = settings
        self._creds = creds
        self._client = ExchangeClient(settings, creds)
        self._market = MarketData(self._client, settings)
        self._executor = Executor(self._client, settings)
        # LLM provider credentials only come from the environment-specific profile DB.
        # An empty chain safely degrades to HOLD until an operator activates a profile.
        from src.llm.failover import LLMFailoverClient
        self._llm = LLMFailoverClient([])
        # 热替换守护：lock 让「关旧 + 开新」与「正在调用 LLM」互斥。
        self._llm_lock = asyncio.Lock()
        # 单调递增，每次成功替换 +1；web 端通过 /api/llm/status.active.version
        # 感知"热替换是否真的发生了"。
        self._llm_version = 0
        self._llm_profile_name = ""
        self._llm_prompt_version = 0
        self._llm_prompt_name = ""
        self._store = Store(settings.storage.db_path)
        self.runtime = RuntimeState()
        self._account = AccountStateCoordinator(
            self._store, self.runtime, settings.account.quote_asset,
        )
        self._notifier = Notifier(
            settings.notify, creds.telegram_bot_token, creds.telegram_chat_id
        )
        self._symbol_enabled = {symbol: True for symbol in settings.symbols}
        self._symbols = list(settings.symbols)
        self._symbol_needs_review: set[str] = set()
        self._user_stream = BinanceUserDataStream(
            self._client, settings, self._on_private_event, self._on_stream_health
        )
        self._executor.set_account_state(self._account)
        self._stopped = asyncio.Event()
        self._state_sync_lock = asyncio.Lock()
        self._just_adopted: dict[str, bool] = {}  # B4: 标记本周期刚接管的 symbol
        self._recent_explicit_closes: dict[str, float] = {}
        self._close_confirmations: dict[str, CloseConfirmationState] = {}
        self._last_sltp_adjust: dict[str, SltpAdjustState] = {}
        self._last_protection_alerts: dict[str, tuple[str, ...]] = {}
        self._reconcile_task: asyncio.Task | None = None
        self._cycle_leader_snapshot: FeatureSnapshot | None = None
        self._risk_defaults = settings.risk.model_copy(deep=True)
        self._risk_version = 0
        self._engine_defaults = engine_defaults_from_settings(settings)
        self._engine_settings = self._engine_defaults.model_copy(deep=True)
        self._engine_version = 0
        self._execution_base = settings.execution.model_copy(deep=True)
        self._execution_defaults = execution_defaults_from_settings(settings)
        self._execution_settings = self._execution_defaults.model_copy(deep=True)
        self._execution_version = 0
        self._stream_halted_entries = False
        self._private_invariant_task: asyncio.Task | None = None
        self._stream_verify_task: asyncio.Task | None = None
        self._buffer_private_events = True
        self._private_event_buffer: list = []
        self._startup_complete = False
        self._last_fill_sync_at = 0.0

    # ---------- 生命周期 ----------
    async def startup(self) -> None:
        await self._store.connect()
        await self._store.prune_exchange_events(self._settings.user_stream.event_retention_days)
        await self._account.start()
        await self._store.sync_config_symbols(self._settings.symbols)
        await self._apply_runtime_settings()
        await self._client.load_markets()
        if self._settings.is_mainnet and not self._settings.execution.attach_sl_tp:
            raise RuntimeError("mainnet requires execution.attach_sl_tp=true")
        try:
            await self._client.validate_account_mode()
        except Exception as e:
            self.runtime.halt_entries(f"account mode validation failed: {e}")
            await self._persist_strategy_pause_reason(
                reason_code="ACCOUNT_MODE_INVALID",
                reason=self.runtime.halt_new_entries_reason,
                source="engine:startup",
            )
            raise
        for symbol in self._symbols:
            filters = await self._client.ensure_symbol(symbol)
            await self._store.update_symbol_filters(symbol, filters)
            self._market.ensure_symbol(symbol)
        await self._market.refresh_all(self._symbols)
        await self._restore_decision_snapshots()
        # 启动时从 DB 重算当日盈亏，避免重启后 day_realized_pnl=0 失真
        # （日亏熔断、前端"当日已实现盈亏"都依赖此值）。失败时退回到 0，
        # 不影响其它启动流程。
        try:
            by_day = await self._store.day_realized_pnl_by_local_day()
            self.runtime.rehydrate_day_pnl(by_day)
            logger.info(
                "day pnl rehydrated from db: day={} pnl={:.4f} (history days={})",
                self.runtime.day_key, self.runtime.day_realized_pnl, len(by_day),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("day pnl rehydrate failed, fallback to 0: {}", e)
            self.runtime.roll_day_if_needed()
        if self._settings.user_stream.enabled:
            try:
                await self._user_stream.start()
                await self._user_stream.wait_connected()
            except Exception as e:
                logger.warning("private user stream startup failed: {}", e)
                if not self.runtime.halt_new_entries:
                    self._stream_halted_entries = True
                self.runtime.halt_entries(f"private user stream unavailable: {e}")
                await self._persist_strategy_pause_reason(
                    reason_code="USER_STREAM_UNAVAILABLE",
                    reason=self.runtime.halt_new_entries_reason,
                    source="engine:startup",
                )
        if self._settings.storage.reconcile_on_start or self._settings.user_stream.enabled:
            try:
                await self._submit_rest_account_snapshot("startup")
                await self._replay_private_event_buffer()
                await self._sync_exchange_fills(force=True)
            except Exception as e:
                logger.warning("startup reconcile failed: {}", e)
                self.runtime.halt_entries(f"startup reconcile failed: {e}")
                await self._persist_strategy_pause_reason(
                    reason_code="STARTUP_RECONCILE_FAILED",
                    reason=self.runtime.halt_new_entries_reason,
                    source="engine:startup",
                )
        self._startup_complete = True
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(), name="exchange-reconciler"
        )
        # 启动末尾：把 DB 里的 active LLM profile 应用上（首次启动时把 yaml 默认
        # 写一条 default profile 进去，便于 web 端识别当前生效配置）。
        try:
            await self._bootstrap_llm_profile()
        except Exception as e:  # noqa: BLE001
            logger.warning("llm profile bootstrap failed: {}", e)
        logger.info("engine started (mode={}, db={}, equity={:.2f})",
                    self._settings.mode.value, self._settings.storage.db_path,
                    self.runtime.current_equity)

    async def _restore_decision_snapshots(self) -> None:
        restored = 0
        for symbol in self._symbols:
            try:
                row = await self._store.latest_decision_snapshot(symbol)
            except Exception as e:
                logger.warning("restore decision snapshot failed {}: {}", symbol, e)
                continue
            if not row:
                continue
            try:
                snap = FeatureSnapshot.model_validate(row["snapshot"])
            except Exception as e:
                logger.warning("invalid stored decision snapshot {}: {}", symbol, e)
                continue
            self.runtime.last_decision_snapshot[symbol] = snap.model_dump(mode="json")
            self.runtime.last_decision_price[symbol] = float(
                snap.last_price or row.get("ref_price") or 0.0
            )
            self.runtime.last_decision_time[symbol] = int(row.get("ts_ms") or snap.ts_ms)
            restored += 1
        if restored:
            logger.info("restored {} decision feature snapshots", restored)

    async def shutdown(self) -> None:
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
            self._reconcile_task = None
        if self._private_invariant_task is not None:
            self._private_invariant_task.cancel()
            try:
                await self._private_invariant_task
            except asyncio.CancelledError:
                pass
            self._private_invariant_task = None
        if self._stream_verify_task is not None:
            self._stream_verify_task.cancel()
            try:
                await self._stream_verify_task
            except asyncio.CancelledError:
                pass
            self._stream_verify_task = None
        await self._user_stream.close()
        await self._account.close()
        await self._market.stop()
        await self._llm.close()
        await self._notifier.close()
        await self._store.close()
        await self._client.close()
        logger.info("engine shutdown complete")

    async def _on_private_event(self, event) -> None:
        if self._buffer_private_events:
            if event.event_type == "MARGIN_CALL":
                self.runtime.halt_entries("Binance margin call: new entries paused")
            if len(self._private_event_buffer) >= self._settings.user_stream.startup_buffer_limit:
                self.runtime.halt_entries("private user stream buffer overflow")
                await self._persist_strategy_pause_reason(
                    reason_code="USER_STREAM_BUFFER_OVERFLOW",
                    reason=self.runtime.halt_new_entries_reason,
                    source="binance:user-stream",
                )
                return
            self._private_event_buffer.append(event)
            return
        await self._apply_private_event(event)

    async def _apply_private_event(self, event) -> None:
        await self._account.submit(event)
        await self._account.drain()
        if event.event_type == "ORDER_TRADE_UPDATE":
            order = event.payload.get("o") or {}
            fill = private_order_trade_fill(event)
            if fill is not None:
                try:
                    position = self.runtime.positions.get(
                        normalize_symbol(fill.get("symbol"))
                    ) or {}
                    fill["leverage"] = int(position.get("leverage") or 0)
                    result = await self._store.ingest_exchange_fill(fill)
                    if result.get("inserted") and result.get("ownership") in (
                        "external", "mixed",
                    ):
                        logger.warning(
                            "{} Binance fill archived ownership={} reason={}",
                            fill["symbol"], result["ownership"], result.get("reason") or "",
                        )
                except Exception as e:
                    logger.error("private Binance fill archive failed: {}", e)
            await self._mark_condition_exit_from_order_update(event, order)
            client_id = str(order.get("c") or "")
            status = str(order.get("X") or "").upper()
            symbol = normalize_symbol(order.get("s"))
            if symbol and client_id and not client_id.startswith("bt-") and status in (
                "NEW", "PARTIALLY_FILLED", "FILLED",
            ):
                await self._set_symbol_disabled(
                    symbol,
                    reason_code="EXTERNAL_ORDER_DETECTED",
                    reason=f"external Binance order detected: {client_id} status={status}",
                    source="binance:user-stream",
                    action="record_only_no_takeover",
                )
        if event.event_type == "MARGIN_CALL":
            self.runtime.halt_entries("Binance margin call: new entries paused")
            await self._persist_strategy_pause_reason(
                reason_code="MARGIN_CALL",
                reason=self.runtime.halt_new_entries_reason,
                source="binance:user-stream",
            )
            await self._notifier.send(Event.ERROR, self.runtime.halt_new_entries_reason)
        elif event.event_type == "ACCOUNT_CONFIG_UPDATE":
            await self._handle_account_config_update(event)
        elif event.event_type == "CONDITIONAL_ORDER_TRIGGER_REJECT":
            self.runtime.halt_entries(f"Binance private event requires review: {event.event_type}")
            await self._persist_strategy_pause_reason(
                reason_code=event.event_type,
                reason=self.runtime.halt_new_entries_reason,
                source="binance:user-stream",
            )
            details = event.payload.get("or") or event.payload.get("o") or {}
            symbol = normalize_symbol(details.get("s") or details.get("symbol"))
            if symbol:
                await self._set_symbol_disabled(
                    symbol,
                    reason_code="CONDITIONAL_ORDER_TRIGGER_REJECT",
                    reason="Binance rejected a conditional protection trigger",
                    source="binance:user-stream",
                    action="repair_or_emergency_close",
                )
            asyncio.create_task(
                self._reconcile_exchange_state(event.event_type.lower()),
                name=f"private-event-reconcile-{event.event_type.lower()}",
            )
        elif event.event_type in ("ACCOUNT_UPDATE", "ORDER_TRADE_UPDATE", "ALGO_UPDATE"):
            if self._private_invariant_task is None or self._private_invariant_task.done():
                self._private_invariant_task = asyncio.create_task(
                    self._enforce_exchange_invariants("private_event"),
                    name="private-event-invariant-check",
                )

    async def _mark_condition_exit_from_order_update(self, event, order: dict) -> None:
        """Persist authoritative Binance private-stream timestamps for SL/TP exits."""
        status = str(order.get("X") or "").upper()
        exec_type = str(order.get("x") or "").upper()
        strategy_type = str(order.get("st") or "").upper()
        algo_id = str(order.get("si") or "")
        if status != "FILLED":
            return
        if exec_type and exec_type not in ("TRADE", "CALCULATED"):
            return
        if strategy_type != "ALGO_CONDITION" and not algo_id:
            return
        symbol = normalize_symbol(order.get("s"))
        if not symbol:
            return
        qty = (
            self._safe_float(order.get("z"))
            or self._safe_float(order.get("l"))
            or self._safe_float(order.get("q"))
        )
        price = (
            self._safe_float(order.get("ap"))
            or self._safe_float(order.get("L"))
            or self._safe_float(order.get("p"))
        )
        if qty <= 0 or price <= 0:
            return
        ts_ms = int(event.transaction_time_ms or event.event_time_ms or time.time() * 1000)
        await self._store.mark_condition_exit(
            symbol=symbol,
            triggered_kind="",
            qty=qty,
            price=price,
            ts_ms=ts_ms,
            exchange_order_id=algo_id,
            client_order_id=str(order.get("c") or ""),
            fee=self._safe_float(order.get("n")),
            fee_asset=str(order.get("N") or ""),
        )
        remaining = await self._cancel_symbol_condition_orders(
            symbol, reason="condition_exit_private_event"
        )
        if remaining:
            details = ", ".join(self._condition_order_label(item) for item in remaining)
            await self._disable_symbol_due_stale_conditions(symbol, details)

    async def _handle_account_config_update(self, event) -> None:
        """Classify Binance account-config events instead of pausing on expected leverage updates."""
        account_config = event.payload.get("ac")
        if isinstance(account_config, dict):
            symbol = normalize_symbol(account_config.get("s") or account_config.get("symbol"))
            try:
                leverage = int(account_config.get("l") or account_config.get("leverage"))
            except (TypeError, ValueError):
                leverage = 0
            if symbol and 0 < leverage <= self._settings.risk.max_leverage:
                logger.info(
                    "Binance leverage configuration updated: {}={}x (within max {}x)",
                    symbol, leverage, self._settings.risk.max_leverage,
                )
                self._schedule_private_event_reconcile("account_config_update_leverage")
                return
            if symbol and leverage > self._settings.risk.max_leverage:
                reason = (
                    f"Binance leverage configuration exceeds hard limit: "
                    f"{symbol}={leverage}x > {self._settings.risk.max_leverage}x"
                )
                await self._set_symbol_disabled(
                    symbol,
                    reason_code="ACCOUNT_CONFIG_LEVERAGE_EXCEEDED",
                    reason=reason,
                    source="binance:user-stream",
                    action="manual_review",
                )
                await self._pause_for_private_event("ACCOUNT_CONFIG_LEVERAGE_EXCEEDED", reason)
                self._schedule_private_event_reconcile("account_config_update_leverage_exceeded")
                return

        account_info = event.payload.get("ai")
        if isinstance(account_info, dict) and isinstance(account_info.get("j"), bool):
            multi_assets_enabled = account_info["j"]
            if not multi_assets_enabled:
                logger.info("Binance multi-assets margin mode confirmed disabled")
                self._schedule_private_event_reconcile("account_config_update_multi_assets_disabled")
                return
            reason = "Binance multi-assets margin mode enabled; mode is unsupported"
            await self._pause_for_private_event("ACCOUNT_CONFIG_MULTI_ASSETS_ENABLED", reason)
            self._schedule_private_event_reconcile("account_config_update_multi_assets_enabled")
            return

        reason = "Binance private event requires review: ACCOUNT_CONFIG_UPDATE"
        await self._pause_for_private_event("ACCOUNT_CONFIG_UPDATE_UNKNOWN", reason)
        self._schedule_private_event_reconcile("account_config_update_unknown")

    async def _pause_for_private_event(self, reason_code: str, reason: str) -> None:
        self.runtime.halt_entries(reason)
        await self._persist_strategy_pause_reason(
            reason_code=reason_code,
            reason=reason,
            source="binance:user-stream",
        )
        await self._notifier.send(Event.ERROR, reason)

    def _schedule_private_event_reconcile(self, reason: str) -> None:
        asyncio.create_task(
            self._reconcile_exchange_state(reason),
            name=f"private-event-reconcile-{reason}",
        )

    async def _replay_private_event_buffer(self) -> None:
        while self._private_event_buffer:
            pending = self._private_event_buffer
            self._private_event_buffer = []
            for event in pending:
                await self._apply_private_event(event)
        self._buffer_private_events = False

    async def _rest_resync_with_event_buffer(self, reason: str) -> None:
        self._buffer_private_events = True
        try:
            await self._submit_rest_account_snapshot(reason)
            await self._replay_private_event_buffer()
        except Exception:
            raise
        if self._stream_halted_entries and self._account.snapshot().stream_status == "LIVE":
            self._stream_halted_entries = False
            self.runtime.resume_entries()
            await self._store.set_runtime_settings({
                "strategy.paused": "false",
                "strategy.pause.reason_code": "",
                "strategy.pause.reason": "",
                "strategy.pause.source": "engine:user-stream-resync",
                "strategy.pause.at_ms": str(int(time.time() * 1000)),
            })

    async def _on_stream_health(self, health: dict) -> None:
        await self._account.set_stream_health(health)
        status = str(health.get("status") or "")
        if status in ("DISCONNECTED", "RESYNCING"):
            if not self.runtime.halt_new_entries:
                self._stream_halted_entries = True
                self.runtime.halt_entries(
                    f"private user stream {status.lower()}: "
                    f"{health.get('reason') or 'resync required'}"
                )
                await self._persist_strategy_pause_reason(
                    reason_code=f"USER_STREAM_{status}",
                    reason=self.runtime.halt_new_entries_reason,
                    source="binance:user-stream",
                )
            if status == "DISCONNECTED":
                if self._stream_verify_task is None or self._stream_verify_task.done():
                    self._stream_verify_task = asyncio.create_task(
                        self._reconcile_exchange_state("user_stream_disconnected"),
                        name="user-stream-disconnect-rest-verify",
                    )
        elif status == "LIVE" and health.get("connected_at_ms") and self._startup_complete:
            self._buffer_private_events = True
            asyncio.create_task(
                self._reconcile_exchange_state("user_stream_connected"),
                name="user-stream-rest-resync",
            )

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
        while not self.runtime.kill_switch and not self._stopped.is_set():
            interval = self._engine_settings.cycle_interval_seconds
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
        self._cycle_leader_snapshot = None

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
            if name in (
                "RESUME",
                "RESUME_ALL_SYMBOLS",
                "UPDATE_ENGINE_SETTINGS",
                "UPDATE_EXECUTION_SETTINGS",
            ):
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
            self.runtime.halt_entries("strategy paused")
            await self._store.set_runtime_setting("strategy.paused", "true")
            await self._notifier.send(Event.CIRCUIT_BREAK, "paused via web (no new entries)")
            return "strategy paused (persisted)"
        if name == "RESUME":
            self._assert_stream_ready_for_entries()
            previous_reason = str(
                await self._store.get_runtime_setting("strategy.pause.reason_code") or ""
            )
            bypass_drawdown = (
                previous_reason == "MAX_DRAWDOWN"
                or self.runtime.risk_day_drawdown_pct
                >= self._settings.risk.max_drawdown_pct
            )
            bypass_day = ""
            if bypass_drawdown:
                bypass_day = self.runtime.grant_drawdown_bypass()
            self.runtime.resume_entries()
            settings = {
                "strategy.paused": "false",
                "strategy.pause.reason_code": "",
                "strategy.pause.reason": "",
                "strategy.pause.source": "",
                "strategy.pause.at_ms": "",
            }
            if bypass_drawdown:
                settings.update({
                    _RISK_DRAWDOWN_BYPASS_DAY_KEY: bypass_day,
                    _RISK_DRAWDOWN_BYPASS_AT_MS_KEY: str(int(time.time() * 1000)),
                })
            await self._store.set_runtime_settings(settings)
            if bypass_drawdown:
                return (
                    f"strategy resumed; daily drawdown breaker bypassed for {bypass_day}; "
                    "daily loss and other safety guards remain active"
                )
            return "strategy resumed (persisted)"
        if name == "UPDATE_RISK_SETTINGS":
            return await self._update_risk_settings(arg)
        if name == "UPDATE_ENGINE_SETTINGS":
            return await self._update_engine_settings(arg)
        if name == "UPDATE_EXECUTION_SETTINGS":
            return await self._update_execution_settings(arg)
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
        if name == "SWITCH_LLM_PROFILE":
            return await self._switch_llm_profile(arg)
        if name == "RELOAD_LLM_PROMPT":
            return await self._reload_llm_prompt()
        if name == "REPAIR_SL_TP":
            return await self._repair_sl_tp(arg)
        if name == "CANCEL_OPEN_ORDER":
            return await self._cancel_open_order(arg)
        if name == "CANCEL_CONDITION_ORDER":
            return await self._cancel_condition_order(arg)
        if name == "CANCEL_ALL_OPEN_ORDERS":
            return await self._cancel_all_open_orders(arg)
        if name == "PROTECT_POSITION":
            return await self._protect_position(arg)
        if name == "CLOSE_POSITION":
            return await self._close_position_command(arg)
        raise ValueError(f"unknown command: {name}")

    async def _apply_runtime_settings(self) -> None:
        """启动时加载持久化运行态；缺省时用 config.yaml 并写入 DB。"""
        await self._reload_symbols_from_store()
        raw_risk = await self._store.get_runtime_setting(RUNTIME_RISK_KEY)
        raw_version = await self._store.get_runtime_setting(RUNTIME_RISK_VERSION_KEY)
        raw_engine = await self._store.get_runtime_setting(RUNTIME_ENGINE_KEY)
        raw_engine_version = await self._store.get_runtime_setting(RUNTIME_ENGINE_VERSION_KEY)
        raw_execution = await self._store.get_runtime_setting(RUNTIME_EXECUTION_KEY)
        raw_execution_version = await self._store.get_runtime_setting(RUNTIME_EXECUTION_VERSION_KEY)
        raw_equity_peak = await self._store.get_runtime_setting("risk.equity_peak")
        raw_risk_day_key = await self._store.get_runtime_setting(_RISK_DAY_KEY)
        raw_risk_day_peak = await self._store.get_runtime_setting(_RISK_DAY_EQUITY_PEAK_KEY)
        raw_drawdown_bypass_day = await self._store.get_runtime_setting(
            _RISK_DRAWDOWN_BYPASS_DAY_KEY
        )
        try:
            self.runtime.equity_peak = max(float(raw_equity_peak or 0.0), 0.0)
        except (TypeError, ValueError):
            self.runtime.halt_entries("invalid persisted equity peak")
        try:
            risk_day_peak = max(float(raw_risk_day_peak or 0.0), 0.0)
        except (TypeError, ValueError):
            risk_day_peak = 0.0
            self.runtime.halt_entries("invalid persisted daily equity peak")
        self.runtime.restore_daily_risk(
            day_key=str(raw_risk_day_key or ""),
            equity_peak=risk_day_peak,
            bypass_day=str(raw_drawdown_bypass_day or ""),
        )
        try:
            self._settings.risk = decode_risk(raw_risk, self._risk_defaults)
            self._risk_version = int(raw_version or 0)
        except Exception as e:
            self.runtime.halt_entries(f"invalid runtime risk settings: {e}")
            logger.error("invalid runtime risk settings; keeping yaml defaults: {}", e)
            self._settings.risk = self._risk_defaults.model_copy(deep=True)
        if raw_risk is None:
            await self._store.set_runtime_settings({
                RUNTIME_RISK_KEY: encode_risk(self._settings.risk),
                RUNTIME_RISK_VERSION_KEY: str(self._risk_version),
            })
        try:
            self._engine_settings = decode_engine(raw_engine, self._engine_defaults)
            self._engine_version = int(raw_engine_version or 0)
        except Exception as e:
            self.runtime.halt_entries(f"invalid runtime engine settings: {e}")
            logger.error("invalid runtime engine settings; keeping yaml defaults: {}", e)
            self._engine_settings = self._engine_defaults.model_copy(deep=True)
        if raw_engine is None:
            await self._store.set_runtime_settings({
                RUNTIME_ENGINE_KEY: encode_engine(self._engine_settings),
                RUNTIME_ENGINE_VERSION_KEY: str(self._engine_version),
            })
        try:
            self._execution_settings = decode_execution(
                raw_execution,
                self._execution_defaults,
                allowed_symbols=set(self._tracked_symbols()),
            )
            self._execution_version = int(raw_execution_version or 0)
            execution_config = execution_runtime_to_config(
                self._execution_settings, self._execution_base
            )
            self._settings.execution = execution_config
            self._executor.apply_execution_config(execution_config)
        except Exception as e:
            self.runtime.halt_entries(f"invalid runtime execution settings: {e}")
            logger.error("invalid runtime execution settings; keeping yaml defaults: {}", e)
            self._execution_settings = self._execution_defaults.model_copy(deep=True)
            self._settings.execution = self._execution_base.model_copy(deep=True)
            self._executor.apply_execution_config(self._settings.execution)
        if raw_execution is None:
            await self._store.set_runtime_settings({
                RUNTIME_EXECUTION_KEY: encode_execution(self._execution_settings),
                RUNTIME_EXECUTION_VERSION_KEY: str(self._execution_version),
            })
        raw_paused = await self._store.get_runtime_setting("strategy.paused")
        if self._settings.is_mainnet:
            self.runtime.halt_entries("mainnet restart guard: manual resume required")
            await self._persist_strategy_pause_reason(
                reason_code="MAINNET_RESTART_GUARD",
                reason=self.runtime.halt_new_entries_reason,
                source="engine:startup",
            )
        elif raw_paused is None:
            await self._store.set_runtime_setting("strategy.paused", "false")
        else:
            paused = raw_paused.strip().lower() in ("1", "true", "yes", "on")
            if paused:
                self.runtime.halt_entries("strategy paused")
            else:
                self.runtime.resume_entries()
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

    async def _update_risk_settings(self, arg: str) -> str:
        payload = json.loads(arg or "{}")
        if not isinstance(payload, dict):
            raise ValueError("UPDATE_RISK_SETTINGS requires a JSON object")
        expected_version = int(payload.pop("expected_version", -1))
        if expected_version != self._risk_version:
            raise ValueError(
                f"risk settings version conflict: expected {expected_version}, "
                f"current {self._risk_version}"
            )
        updated = validate_risk_payload(payload, self._settings.risk)
        self._settings.risk = updated
        self._risk_version += 1
        await self._store.set_runtime_settings({
            RUNTIME_RISK_KEY: encode_risk(updated),
            RUNTIME_RISK_VERSION_KEY: str(self._risk_version),
        })
        tripped = await self._check_circuit_breaker()
        return json.dumps({
            "version": self._risk_version,
            "effective": risk_public(updated),
            "breaker_tripped": tripped,
        }, sort_keys=True)

    async def _update_engine_settings(self, arg: str) -> str:
        payload = json.loads(arg or "{}")
        if not isinstance(payload, dict):
            raise ValueError("UPDATE_ENGINE_SETTINGS requires a JSON object")
        expected_version = int(payload.pop("expected_version", -1))
        if expected_version != self._engine_version:
            raise ValueError(
                f"engine settings version conflict: expected {expected_version}, "
                f"current {self._engine_version}"
            )
        updated = validate_engine_payload(payload, self._engine_settings)
        self._engine_settings = updated
        self._engine_version += 1
        await self._store.set_runtime_settings({
            RUNTIME_ENGINE_KEY: encode_engine(updated),
            RUNTIME_ENGINE_VERSION_KEY: str(self._engine_version),
        })
        return json.dumps({
            "version": self._engine_version,
            "effective": engine_public(updated),
        }, sort_keys=True)

    async def _update_execution_settings(self, arg: str) -> str:
        payload = json.loads(arg or "{}")
        if not isinstance(payload, dict):
            raise ValueError("UPDATE_EXECUTION_SETTINGS requires a JSON object")
        expected_version = int(payload.pop("expected_version", -1))
        if expected_version != self._execution_version:
            raise ValueError(
                f"execution settings version conflict: expected {expected_version}, "
                f"current {self._execution_version}"
            )
        updated = validate_execution_payload(
            payload,
            self._execution_settings,
            allowed_symbols=set(self._tracked_symbols()),
        )
        execution_config = execution_runtime_to_config(updated, self._execution_base)
        self._execution_settings = updated
        self._settings.execution = execution_config
        self._executor.apply_execution_config(execution_config)
        self._execution_version += 1
        await self._store.set_runtime_settings({
            RUNTIME_EXECUTION_KEY: encode_execution(updated),
            RUNTIME_EXECUTION_VERSION_KEY: str(self._execution_version),
        })
        return json.dumps({
            "version": self._execution_version,
            "effective": execution_public(updated),
        }, sort_keys=True)

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
        await self._store.set_symbol_enabled(
            symbol,
            enabled,
            reason_code="" if enabled else "MANUAL_DISABLED",
            reason="" if enabled else "manual disabled via SET_SYMBOL_ENABLED",
            source="web:admin",
            action="manual_toggle",
        )
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
        self._assert_stream_ready_for_entries()
        await self._assert_exchange_clear_for_resume_all()
        previous_reason = str(
            await self._store.get_runtime_setting("strategy.pause.reason_code") or ""
        )
        bypass_drawdown = (
            previous_reason == "MAX_DRAWDOWN"
            or self.runtime.risk_day_drawdown_pct
            >= self._settings.risk.max_drawdown_pct
        )
        bypass_day = self.runtime.grant_drawdown_bypass() if bypass_drawdown else ""
        rows = await self._store.list_symbols()
        eligible = [
            normalize_symbol(row["symbol"])
            for row in rows
            if row.get("status") == "active" and not row.get("needs_review")
        ]
        values = {
            "strategy.paused": "false",
            "strategy.pause.reason_code": "",
            "strategy.pause.reason": "",
            "strategy.pause.source": "",
            "strategy.pause.at_ms": "",
        }
        values.update({
            self._symbol_enabled_key(symbol): "true"
            for symbol in eligible
        })
        if bypass_drawdown:
            values.update({
                _RISK_DRAWDOWN_BYPASS_DAY_KEY: bypass_day,
                _RISK_DRAWDOWN_BYPASS_AT_MS_KEY: str(int(time.time() * 1000)),
            })
        await self._store.set_runtime_settings(values)
        for symbol in eligible:
            await self._store.set_symbol_enabled(symbol, True)
        self.runtime.resume_entries()
        for symbol in eligible:
            self._symbol_enabled[symbol] = True
        await self._reload_symbols_from_store()
        symbols = ", ".join(eligible)
        return (
            f"strategy resumed; enabled all symbols: {symbols}; "
            "precheck passed (no live positions/open orders/condition orders)"
            + (
                f"; daily drawdown breaker bypassed for {bypass_day}"
                if bypass_drawdown else ""
            )
        )

    def _assert_stream_ready_for_entries(self) -> None:
        if not self._settings.user_stream.enabled or not self._account.started:
            return
        status = self._account.snapshot().stream_status
        if status != "LIVE":
            raise ValueError(f"private user stream is not ready: {status}")

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

    async def _set_symbol_disabled(
        self,
        symbol: str,
        *,
        reason_code: str,
        reason: str,
        source: str,
        action: str,
    ) -> None:
        symbol = normalize_symbol(symbol)
        self._symbol_enabled[symbol] = False
        await self._store.set_symbol_enabled(
            symbol,
            False,
            reason_code=reason_code,
            reason=reason,
            source=source,
            action=action,
        )
        logger.error(
            "symbol disabled {} reason_code={} source={} action={} reason={}",
            symbol, reason_code, source, action, reason,
        )

    async def _persist_strategy_pause_reason(
        self,
        *,
        reason_code: str,
        reason: str,
        source: str,
    ) -> None:
        await self._store.set_runtime_settings({
            "strategy.paused": "true",
            "strategy.pause.reason_code": reason_code,
            "strategy.pause.reason": reason,
            "strategy.pause.source": source,
            "strategy.pause.at_ms": str(int(time.time() * 1000)),
        })

    async def _cancel_and_flatten(self, source: str) -> str:
        """撤销挂单并平掉当前持仓，但不停止交易引擎。"""
        self.runtime.halt_entries("strategy paused after cancel+flatten")
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

    async def _cancel_open_order(self, arg: str) -> str:
        """撤销一条普通未成交挂单（限价/限价 maker 等，不含 SL/TP 算法单）。

        arg 为 JSON: ``{"symbol": "BTCUSDT", "order_id": "..."}``
        ``order_id`` 缺失时回退到 ``client_order_id``。
        """
        try:
            payload = json.loads(arg or "{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"CANCEL_OPEN_ORDER requires JSON arg: {e}") from e
        symbol = normalize_symbol(str(payload.get("symbol") or ""))
        if not symbol:
            raise ValueError("CANCEL_OPEN_ORDER requires symbol")
        order_id = str(payload.get("order_id") or "")
        client_order_id = str(payload.get("client_order_id") or "")
        if not order_id and not client_order_id:
            raise ValueError("CANCEL_OPEN_ORDER requires order_id or client_order_id")
        if hasattr(self._executor, "_cancel_regular_order_safe"):
            try:
                res = await self._executor._cancel_regular_order_safe(
                    symbol, order_id or client_order_id
                )
            except Exception as e:
                raise RuntimeError(f"cancel open order failed: {e}") from e
        else:
            try:
                res = await self._client.cancel_order(
                    symbol,
                    order_id or client_order_id,
                    params={"clientOrderId": client_order_id} if (not order_id and client_order_id) else None,
                )
            except Exception as e:
                raise RuntimeError(f"cancel open order failed: {e}") from e
        if order_id:
            await self._store.mark_orders_status_by_exchange_ids([order_id], "canceled")
        status = (res or {}).get("status") or "submitted"
        return f"{symbol} 已撤销挂单 {order_id or client_order_id} (status={status})"

    async def _cancel_condition_order(self, arg: str) -> str:
        """撤销一条条件单（SL/TP 算法单）。

        arg 为 JSON: ``{"symbol": "BTCUSDT", "algo_id": "...", "client_algo_id": "..."}``
        """
        try:
            payload = json.loads(arg or "{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"CANCEL_CONDITION_ORDER requires JSON arg: {e}") from e
        symbol = normalize_symbol(str(payload.get("symbol") or ""))
        if not symbol:
            raise ValueError("CANCEL_CONDITION_ORDER requires symbol")
        algo_id = str(payload.get("algo_id") or payload.get("order_id") or "")
        client_algo_id = str(payload.get("client_algo_id") or "")
        if not algo_id and not client_algo_id:
            raise ValueError("CANCEL_CONDITION_ORDER requires algo_id or client_algo_id")
        try:
            res = await self._client.cancel_condition_order(
                symbol,
                algo_id,
                client_algo_id=client_algo_id,
            )
        except Exception as e:
            raise RuntimeError(f"cancel condition order failed: {e}") from e
        if algo_id:
            await self._store.mark_orders_status_by_exchange_ids([algo_id], "canceled")
        status = (res or {}).get("status") or "submitted"
        return f"{symbol} 已撤销条件单 {algo_id or client_algo_id} (status={status})"

    async def _cancel_all_open_orders(self, arg: str) -> str:
        """撤销某 symbol 所有普通挂单；条件单走 CANCEL_CONDITION_ORDER。"""
        symbol = normalize_symbol(str(arg or "").strip())
        if not symbol:
            raise ValueError("CANCEL_ALL_OPEN_ORDERS requires a symbol")
        try:
            await self._client.cancel_all_orders(symbol=symbol)
        except Exception as e:
            raise RuntimeError(f"cancel all open orders failed: {e}") from e
        await asyncio.sleep(0.3)
        try:
            remaining = await self._client.fetch_open_orders(symbol)
        except Exception as e:
            logger.warning("post-cancel fetch open orders {} failed: {}", symbol, e)
            remaining = []
        canceled_ids = {str(o.get("id") or "") for o in remaining if o.get("id")}
        return f"{symbol} 已批量撤销普通挂单；剩余未撤 {len(remaining)}"

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
                leverage=int(position.get("leverage") or 0),
            )
            if reason:
                if kind == "SL":
                    crossed_mark = (
                        (side == "long" and trigger >= mark)
                        or (side == "short" and trigger <= mark)
                    )
                    if crossed_mark:
                        raise ProtectionRepairError(
                            symbol,
                            (
                                f"{symbol}: SL trigger crossed current mark; "
                                f"side={side} sl={trigger:.2f} mark={mark:.2f}; {reason}"
                            ),
                            reason_code="SL_TRIGGER_CROSSED_MARK",
                        )
                rejected.append(f"{kind}@{trigger:.2f}: {reason}")
                continue
            otype = "STOP_MARKET" if kind == "SL" else "TAKE_PROFIT_MARKET"
            specs.append((kind, otype, trigger))
            accepted.append(f"{kind}@{trigger:.2f}({source})")

        if not specs:
            raise ProtectionRepairError(
                symbol,
                f"{symbol}: 未补挂保护单；" + "；".join(rejected),
            )

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

    async def _protect_position(self, arg: str) -> str:
        """人工确认后，为当前交易所剩余/接管持仓按新触发价挂保护单。"""
        try:
            payload = json.loads(arg or "{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"PROTECT_POSITION requires JSON arg: {e}") from e
        if not isinstance(payload, dict):
            raise ValueError("PROTECT_POSITION arg must be a JSON object")
        if not bool(payload.get("confirm")):
            raise ValueError("PROTECT_POSITION requires confirm=true")

        symbol = normalize_symbol(str(payload.get("symbol") or ""))
        if not symbol:
            raise ValueError("PROTECT_POSITION requires symbol")
        if symbol not in self._tracked_symbols():
            raise ValueError(f"symbol not registered: {symbol}")

        position = await self._fetch_exchange_position(symbol)
        if position is None:
            return f"{symbol}: 交易所当前无持仓，不需要接管保护"
        side = position["side"]
        live_qty = float(position["contracts"] or 0.0)
        entry = float(position["entry_price"] or 0.0)
        mark = await self._current_mark_price(symbol, position)
        position["mark_price"] = mark
        if side not in ("long", "short") or live_qty <= 0 or entry <= 0 or mark <= 0:
            raise ValueError(
                f"{symbol}: 持仓数据不完整，无法接管保护 "
                f"(side={side}, qty={live_qty}, entry={entry}, mark={mark})"
            )

        observed = payload.get("position") if isinstance(payload.get("position"), dict) else {}
        self._validate_position_signature(
            symbol=symbol,
            side=side,
            qty=live_qty,
            entry=entry,
            observed=observed,
        )

        target_qty = float(payload.get("qty") or live_qty)
        qty_tol = max(abs(live_qty) * 1e-6, 1e-12)
        if target_qty <= 0 or target_qty - live_qty > qty_tol:
            raise ValueError(f"{symbol}: 接管数量 {target_qty:g} 必须大于 0 且不超过当前持仓 {live_qty:g}")

        close_side = "sell" if side == "long" else "buy"
        active_orders = await self._active_protection_orders(symbol)
        replace_external = bool(payload.get("replace_external"))
        takeover_old_orders = [
            order for order in active_orders
            if str(order.get("origin") or "EXTERNAL") != "ENGINE"
        ] if replace_external else []
        await self._sync_condition_history(symbol, active_orders)
        stale = self._stale_protection_orders(
            active_orders,
            side=side,
            close_side=close_side,
            qty=target_qty,
            entry=entry,
            mark=mark,
        )
        if stale:
            remaining = await self._cancel_stale_condition_orders(
                symbol=symbol,
                orders=[order for order, _reason in stale],
                reason="protect_position",
            )
            if remaining:
                details = ", ".join(self._condition_order_label(order) for order in remaining)
                await self._disable_symbol_due_stale_conditions(symbol, details)
                raise ValueError(f"{symbol}: 存在无法撤销的陈旧条件单，已禁用该标的新开仓: {details}")
            active_orders = await self._active_protection_orders(symbol)

        equity = await self._current_equity()
        specs: list[ProtectionOrderSpec] = []
        skipped: list[str] = []
        errors: list[str] = []
        triggers = self._manual_protection_triggers(payload, side=side, entry=entry, mark=mark)
        if not triggers.get("SL") and not self._has_active_protection(
            active_orders, kind="SL", close_side=close_side, side=side,
            qty=target_qty, entry=entry, mark=mark,
        ):
            raise ValueError(f"{symbol}: 当前缺少止损，接管保护必须提供新的 SL trigger")

        filters = self._client.filters(symbol)
        requested = [
            ProtectionOrderSpec(
                kind="SL",
                order_type="STOP_MARKET",
                trigger_price=float(triggers.get("SL") or 0.0),
                qty=target_qty,
                leg_id="SL",
            )
        ]
        tp_targets = payload.get("take_profit_targets")
        if isinstance(tp_targets, list) and tp_targets:
            if len(tp_targets) > 3:
                errors.append("分批止盈最多支持 3 档")
            for index, target in enumerate(tp_targets[:3], start=1):
                if not isinstance(target, dict):
                    errors.append(f"TP{index}: 格式必须是对象")
                    continue
                position_pct = float(target.get("position_pct") or 0.0)
                leg_qty = float(target.get("qty") or 0.0)
                if leg_qty <= 0 and position_pct > 0:
                    leg_qty = target_qty * position_pct
                if position_pct > 1:
                    errors.append(f"TP{index}: position_pct 不能超过 1")
                    continue
                if leg_qty <= 0:
                    errors.append(f"TP{index}: 必须提供 qty 或 position_pct")
                    continue
                requested.append(ProtectionOrderSpec(
                    kind="TP",
                    order_type="TAKE_PROFIT_MARKET",
                    trigger_price=float(
                        target.get("trigger_price") or target.get("price") or 0.0
                    ),
                    qty=leg_qty,
                    leg_id=str(target.get("leg_id") or f"TP{index}"),
                    position_pct=position_pct if position_pct > 0 else None,
                ))
        else:
            requested.append(ProtectionOrderSpec(
                kind="TP",
                order_type="TAKE_PROFIT_MARKET",
                trigger_price=float(triggers.get("TP") or 0.0),
                qty=target_qty,
                leg_id="TP1",
                position_pct=1.0,
            ))

        tp_requested_qty = sum(
            float(spec.qty or 0.0) for spec in requested if spec.kind == "TP"
        )
        if tp_requested_qty - target_qty > qty_tol:
            errors.append(
                f"TP 分批数量合计 {tp_requested_qty:g} 超过接管数量 {target_qty:g}"
            )

        for spec in requested:
            kind = spec.kind
            trigger = float(spec.trigger_price or 0.0)
            if trigger <= 0:
                continue
            trigger = float(round_price(trigger, filters))
            if not replace_external and self._has_active_protection(
                active_orders,
                kind=kind,
                close_side=close_side,
                side=side,
                qty=target_qty,
                entry=entry,
                mark=mark,
            ):
                skipped.append(f"{kind}: 已有有效条件单")
                continue
            spec_qty = float(spec.qty or target_qty)
            reason = self._validate_repair_trigger(
                symbol=symbol,
                side=side,
                kind=kind,
                trigger=trigger,
                entry=entry,
                mark=mark,
                qty=spec_qty,
                equity=equity,
                leverage=int(position.get("leverage") or 0),
            )
            if reason:
                errors.append(f"{spec.leg_id or kind}@{trigger:.2f}: {reason}")
                continue
            if normalize_order(qty=spec_qty, price=trigger, f=filters, is_market=True) is None:
                errors.append(
                    f"{spec.leg_id or kind}@{trigger:.2f}: 数量/名义价值低于交易所最小限制"
                )
                continue
            specs.append(ProtectionOrderSpec(
                kind=kind,
                order_type=spec.order_type,
                trigger_price=trigger,
                qty=spec_qty,
                leg_id=spec.leg_id,
                position_pct=spec.position_pct,
            ))

        if errors:
            raise ValueError(f"{symbol}: 接管保护参数无效；" + "；".join(errors))
        if not specs:
            return f"{symbol}: 无需补挂新保护单" + (f"；{'; '.join(skipped)}" if skipped else "")

        trade_id = 0
        managed_qty = await self._store.open_trade_qty(symbol)
        managed_tol = max(abs(target_qty) * 1e-6, 1e-12)
        takeover_qty = 0.0
        if managed_qty <= managed_tol:
            takeover_qty = target_qty
        elif target_qty - managed_qty > managed_tol:
            takeover_qty = target_qty - managed_qty
        if takeover_qty > managed_tol:
            trade_id = await self._store.ensure_takeover_trade(
                symbol=symbol,
                direction=side,
                qty=takeover_qty,
                entry_price=entry,
                leverage=int(position.get("leverage") or 0),
            )

        results = await self._executor.place_protection_orders(
            symbol=symbol,
            pos_side=side,
            qty=target_qty,
            specs=specs,
        )
        for order in results:
            if trade_id > 0 and abs(takeover_qty - target_qty) <= managed_tol:
                order["trade_id"] = trade_id
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
            raise ValueError(f"{symbol}: 接管保护下发失败；" + "；".join(failed))
        if replace_external:
            sl_requested = any(spec.kind == "SL" for spec in specs)
            sl_placed = any(
                order.get("kind") == "SL" and order.get("status") == "placed"
                for order in results
            )
            if sl_requested and not sl_placed:
                raise ValueError(f"{symbol}: 新止损未成功挂出，保留原外部保护单")
            remaining = await self._cancel_stale_condition_orders(
                symbol=symbol,
                orders=takeover_old_orders,
                reason="explicit_protection_takeover",
            )
            if remaining:
                details = ", ".join(self._condition_order_label(order) for order in remaining)
                await self._disable_symbol_due_stale_conditions(symbol, details)
                raise ValueError(f"{symbol}: 新保护已挂出，但旧外部条件单未完全撤销: {details}")
        parts = [f"{symbol}: 已接管保护 {', '.join(placed)}"]
        if replace_external and takeover_old_orders:
            parts.append(f"已替换 {len(takeover_old_orders)} 个外部条件单")
        if skipped:
            parts.append("跳过: " + "；".join(skipped))
        if failed:
            parts.append("下发失败: " + "；".join(failed))
        return "；".join(parts)

    async def _close_position_command(self, arg: str) -> str:
        """人工确认后，按交易所当前持仓执行 reduce-only 市价平仓。"""
        try:
            payload = json.loads(arg or "{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"CLOSE_POSITION requires JSON arg: {e}") from e
        if not isinstance(payload, dict):
            raise ValueError("CLOSE_POSITION arg must be a JSON object")
        if not bool(payload.get("confirm")):
            raise ValueError("CLOSE_POSITION requires confirm=true")

        symbol = normalize_symbol(str(payload.get("symbol") or ""))
        if not symbol:
            raise ValueError("CLOSE_POSITION requires symbol")
        if symbol not in self._tracked_symbols():
            raise ValueError(f"symbol not registered: {symbol}")

        raw_position = await self._fetch_exchange_position_raw(symbol)
        if raw_position is None:
            remaining = await self._cancel_symbol_condition_orders(
                symbol, reason="manual_close_flat"
            )
            closed = await self._store.reconcile_symbol_flat(
                symbol, reason="MANUAL_CLOSE"
            )
            parts = [f"{symbol}: 交易所当前无持仓，不需要平仓"]
            if closed:
                parts.append(f"已修正本地 open trade {closed} 条")
            if remaining:
                details = ", ".join(self._condition_order_label(order) for order in remaining)
                parts.append(f"仍有条件单未撤销: {details}")
            return "；".join(parts)

        position = normalize_position(raw_position)
        side = position["side"]
        qty = float(position["contracts"] or 0.0)
        entry = float(position["entry_price"] or 0.0)
        mark = await self._current_mark_price(symbol, position)
        if side not in ("long", "short") or qty <= 0 or entry <= 0:
            raise ValueError(
                f"{symbol}: 持仓数据不完整，无法手动平仓 "
                f"(side={side}, qty={qty}, entry={entry})"
            )
        observed = payload.get("position") if isinstance(payload.get("position"), dict) else {}
        self._validate_position_signature(
            symbol=symbol,
            side=side,
            qty=qty,
            entry=entry,
            observed=observed,
        )

        estimate = realized_pnl(side=side, entry_price=entry, exit_price=mark, qty=qty)
        # 手动平仓是用户确认后的强制退出路径：仍保留交易所持仓重拉、
        # 页面持仓签名校验和 reduce-only，但绕过普通市价滑点预检。
        result = await self._executor.close_position(
            raw_position,
            mode=ExecutionMode.MARKET_TAKER,
            skip_slippage_guard=True,
        )
        raw = result.get("raw") if isinstance(result.get("raw"), dict) else {}
        local_meta = raw.get("_local") if isinstance(raw.get("_local"), dict) else {}
        result["raw"] = {
            **raw,
            "_local": {
                **local_meta,
                "manual_force_close": True,
                "slippage_guard_skipped": True,
            },
        }
        await self._store.log_order(result)
        if not result.get("filled"):
            raise ValueError(f"{symbol}: 手动平仓未成交 status={result.get('status')} raw={result.get('raw')}")

        pnl = realized_pnl(
            side=result.get("pos_side", side),
            entry_price=result.get("entry_price", entry),
            exit_price=result.get("price", 0.0),
            qty=result.get("qty", 0.0),
        )
        self.runtime.add_realized_pnl(pnl)
        self.runtime.mark_order_event(symbol)
        remaining_position = await self._fetch_exchange_position(symbol)
        if remaining_position is not None:
            remaining_qty = float(remaining_position.get("contracts") or 0.0)
            await self._notifier.send(
                Event.CLOSE,
                f"{symbol} manual close partial qty={result.get('qty')} "
                f"remaining={remaining_qty:g} pnl={pnl:.2f}",
            )
            return (
                f"{symbol}: 手动平仓部分成交 qty={float(result.get('qty') or 0.0):g} "
                f"avg={float(result.get('price') or 0.0):.4f} "
                f"pnl={pnl:.2f} USDT；剩余持仓 {remaining_qty:g}，保护单未自动撤销"
            )

        self.runtime.positions.pop(symbol, None)
        remaining = await self._cancel_symbol_condition_orders(
            symbol, reason="manual_close"
        )
        await self._store.reconcile_symbol_flat(
            symbol,
            reason="MANUAL_CLOSE",
            exchange_trades_provider=self._fetch_exit_trades,
        )
        canceled_note = "保护单已撤销" if not remaining else (
            "仍有条件单未撤销: "
            + ", ".join(self._condition_order_label(order) for order in remaining)
        )
        await self._notifier.send(
            Event.CLOSE,
            f"{symbol} manual closed qty={result.get('qty')} pnl={pnl:.2f}",
        )
        return (
            f"{symbol}: 手动平仓完成 qty={float(result.get('qty') or 0.0):g} "
            f"avg={float(result.get('price') or 0.0):.4f} "
            f"pnl={pnl:.2f} USDT "
            f"(提交前估算 {estimate:.2f} USDT)；{canceled_note}"
        )

    @staticmethod
    def _validate_position_signature(
        *,
        symbol: str,
        side: str,
        qty: float,
        entry: float,
        observed: dict,
    ) -> None:
        if not observed:
            return
        obs_side = str(observed.get("side") or "").lower()
        obs_qty = float(observed.get("qty") or observed.get("contracts") or 0.0)
        obs_entry = float(observed.get("entry") or observed.get("entry_price") or 0.0)
        qty_tol = max(abs(qty) * 1e-6, 1e-12)
        entry_tol = max(abs(entry) * 1e-6, 1e-8)
        if obs_side and obs_side != side:
            raise ValueError(f"{symbol}: 页面持仓方向已过期，请刷新后重试")
        if obs_qty > 0 and abs(obs_qty - qty) > qty_tol:
            raise ValueError(f"{symbol}: 页面持仓数量已过期，请刷新后重试")
        if obs_entry > 0 and abs(obs_entry - entry) > entry_tol:
            raise ValueError(f"{symbol}: 页面开仓价已过期，请刷新后重试")

    @staticmethod
    def _manual_protection_triggers(
        payload: dict,
        *,
        side: str,
        entry: float,
        mark: float,
    ) -> dict[str, float]:
        mode = str(payload.get("mode") or "manual").lower()
        if mode == "recompute":
            sl_pct = float(payload.get("stop_loss_pct") or payload.get("sl_pct") or 0.0)
            tp_pct = float(payload.get("take_profit_pct") or payload.get("tp_pct") or 0.0)
            out: dict[str, float] = {}
            if sl_pct > 0:
                out["SL"] = mark * (1 - sl_pct) if side == "long" else mark * (1 + sl_pct)
            if tp_pct > 0:
                out["TP"] = entry * (1 + tp_pct) if side == "long" else entry * (1 - tp_pct)
            return out
        return {
            "SL": float(payload.get("sl_trigger") or payload.get("sl") or 0.0),
            "TP": float(payload.get("tp_trigger") or payload.get("tp") or 0.0),
        }

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

    async def _fetch_exit_trades(
        self, symbol: str, since_ms: int, until_ms: int
    ) -> list[dict]:
        """反查交易所 myTrades，用于 EXCHANGE_FLAT/MANUAL_CLOSE 路径补真实平仓均价。

        拉取 ``[since_ms, until_ms]`` 窗口内该 symbol 的成交；ccxt 偶尔会带
        略早的少量样本，调用方（如 ``reconcile_symbol_flat``）会再次按开仓
        时间过滤。失败时返回空列表（store 兜底用 entry_price）。
        """
        try:
            ccxt_sym = self._client._to_ccxt_symbol(symbol)
        except Exception:
            return []
        try:
            trades = await self._client.raw.fetch_my_trades(
                ccxt_sym, since=int(since_ms), limit=1000
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("{} fetch exit trades failed: {}", symbol, e)
            return []
        return [t for t in (trades or []) if int(t.get("timestamp") or 0) <= int(until_ms)]

    async def _precheck_before_attach_sl_tp(
        self,
        *,
        symbol: str,
        decision: TradeDecision,
        open_result: dict,
    ) -> dict[str, Any]:
        """C2：下 SL/TP 之前做交易所侧二次确认。

        检查项：
        1) 持仓方向/数量与本地 OPEN 结果一致（防 race 后被自动减仓 / 仓位已
           被外部 close）。
        2) 同方向、同种类的 reduce-only 条件单还没挂（防重复挂）。
        3) entry_price 漂移在合理范围内（防均价差异导致触发价乱算）。

        返回 {ok: bool, qty, entry, reason}
        """
        from src.exchange.positions import normalize_position as _np
        side = "long" if decision.action == Action.OPEN_LONG else "short"
        close_side = "sell" if side == "long" else "buy"
        expected_qty = float(open_result.get("qty") or 0.0)
        expected_entry = float(open_result.get("price") or 0.0)
        # 1) 拉交易所侧实情。市价成交后 positionRisk/positions 可能有短暂刷新延迟；
        # 对“暂未看到持仓”做一个短确认窗口，避免误判成裸仓保护失败。
        live = None
        last_reason = ""
        attempts = len(_POST_OPEN_POSITION_CONFIRM_DELAYS_SECONDS)
        for attempt, delay in enumerate(_POST_OPEN_POSITION_CONFIRM_DELAYS_SECONDS, start=1):
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                raw_positions = await self._client.fetch_positions([symbol])
            except Exception as e:
                last_reason = f"fetch_positions failed: {e}"
                logger.warning(
                    "{} pre-attach position confirm attempt {}/{} failed: {}",
                    symbol, attempt, attempts, e,
                )
                continue
            live = next((_np(r) for r in raw_positions if _np(r)["symbol"] == symbol), None)
            if live is not None and live["contracts"] > 0:
                if attempt > 1:
                    logger.warning(
                        "{} pre-attach position appeared after {}/{} attempts",
                        symbol, attempt, attempts,
                    )
                break
            last_reason = f"{symbol} 交易所侧暂未显示持仓"
            if attempt < attempts:
                logger.warning(
                    "{} pre-attach position not visible attempt {}/{}; retry",
                    symbol, attempt, attempts,
                )
        if live is None or live["contracts"] <= 0:
            reason = (
                f"{symbol} 交易所侧在 {attempts} 次确认后仍无持仓，跳过 SL/TP"
                if not last_reason.startswith("fetch_positions failed")
                else f"{last_reason}，跳过 SL/TP"
            )
            return {"ok": False, "reason": reason}
        if live["side"] != side:
            return {
                "ok": False,
                "reason": f"{symbol} 持仓方向 {live['side']} 与决策 {side} 不一致",
            }
        live_qty = live["contracts"]
        qty_tol = max(expected_qty * 1e-6, 1e-12)
        if abs(live_qty - expected_qty) > qty_tol:
            logger.warning(
                "{} pre-attach qty drift: local={} exchange={} (use exchange value)",
                symbol, expected_qty, live_qty,
            )
        live_entry = live["entry_price"]
        # 2) 检查已挂条件单
        try:
            live_conds = await self._client.fetch_open_condition_orders(symbol)
        except Exception as e:
            logger.warning("{} pre-attach fetch_open_condition_orders failed: {}", symbol, e)
            live_conds = []
        active_sl_tp = [
            o for o in live_conds
            if o.get("side", "").lower() == close_side.lower()
            and o.get("type", "").upper() in ("STOP_MARKET", "TAKE_PROFIT_MARKET")
            and o.get("status") in ("open", "placed", "new")
        ]
        if active_sl_tp:
            details = ", ".join(
                f"{o.get('type', '').upper()}@{o.get('stopPrice') or o.get('price')}"
                for o in active_sl_tp
            )
            return {
                "ok": False,
                "reason": f"已存在 {len(active_sl_tp)} 个活跃条件单 ({details})，避免重复挂",
            }
        return {"ok": True, "qty": live_qty, "entry": live_entry, "side": side}

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

        await self._handle_unprotected_open_failure(
            decision=decision,
            open_result=open_result,
            reason="SL protection was not confirmed after open",
            protection_orders=protection_orders,
        )

    def _planned_protection_specs(
        self,
        decision: TradeDecision,
        entry_price: float,
        qty: float | None = None,
    ) -> list[ProtectionOrderSpec]:
        """复用 executor 的触发价规则，提前做本地最小量校验。"""
        symbol = decision.symbol
        is_long = decision.action == Action.OPEN_LONG
        filters = self._client.filters(symbol)
        specs: list[ProtectionOrderSpec] = []
        if decision.stop_loss_pct > 0:
            raw = entry_price * (1 - decision.stop_loss_pct) if is_long else entry_price * (1 + decision.stop_loss_pct)
            specs.append(ProtectionOrderSpec(
                "SL", "STOP_MARKET", float(round_price(raw, filters)),
                qty=qty, leg_id="SL",
            ))
        allocated_qty = 0.0
        targets = decision.effective_take_profit_targets
        for index, target in enumerate(targets, start=1):
            target_qty = None
            if qty is not None:
                is_last = index == len(targets)
                if is_last and sum(item.position_pct for item in targets) >= 1.0 - 1e-9:
                    target_qty = max(0.0, qty - allocated_qty)
                else:
                    from src.exchange.filters import round_qty
                    target_qty = float(round_qty(qty * target.position_pct, filters))
                allocated_qty += target_qty
            raw = (
                entry_price * (1 + target.price_distance_pct)
                if is_long
                else entry_price * (1 - target.price_distance_pct)
            )
            specs.append(ProtectionOrderSpec(
                "TP", "TAKE_PROFIT_MARKET", float(round_price(raw, filters)),
                qty=target_qty,
                leg_id=target.leg_id or f"TP{index}",
                position_pct=target.position_pct,
            ))
        return specs

    def _protection_specs_reject_reason(
        self,
        *,
        symbol: str,
        qty: float,
        specs: list[ProtectionOrderSpec | tuple[str, str, float]],
    ) -> str:
        if not specs:
            return ""
        filters = self._client.filters(symbol)
        for spec in specs:
            if isinstance(spec, tuple):
                kind, _otype, trigger = spec
                spec_qty = qty
            else:
                kind, trigger = spec.kind, spec.trigger_price
                spec_qty = float(spec.qty if spec.qty is not None else qty)
            norm = normalize_order(qty=spec_qty, price=trigger, f=filters, is_market=True)
            if norm is None:
                return f"{kind} qty={spec_qty:g} trigger={trigger:g} below minQty/minNotional"
        return ""

    async def _handle_unprotected_open_failure(
        self,
        *,
        decision: TradeDecision,
        open_result: dict,
        reason: str,
        protection_orders: list[dict] | None = None,
    ) -> None:
        """系统成交后无法确认 SL 时，撤保护残单并只平掉本次成交数量。"""
        symbol = decision.symbol
        await self._disable_symbol_due_protection_failure(symbol, reason)
        if protection_orders:
            remaining = await self._cancel_symbol_condition_orders(
                symbol, reason="protection_failed_after_open"
            )
            if remaining:
                details = ", ".join(self._condition_order_label(order) for order in remaining)
                await self._disable_symbol_due_stale_conditions(symbol, details)
        await self._close_open_result_unprotected(
            decision=decision,
            open_result=open_result,
            reason=reason,
        )

    async def _close_open_result_unprotected(
        self,
        *,
        decision: TradeDecision,
        open_result: dict,
        reason: str,
    ) -> None:
        qty = abs(float(open_result.get("qty") or 0.0))
        price = float(open_result.get("price") or 0.0)
        if qty <= 0:
            return
        side = "long" if decision.action == Action.OPEN_LONG else "short"
        raw_position = {
            "symbol": decision.symbol,
            "side": side,
            "contracts": qty,
            "entryPrice": price,
            "markPrice": price,
        }
        result = await self._executor.close_position(
            raw_position,
            mode=ExecutionMode.MARKET_TAKER,
        )
        await self._store.log_order(result)
        if not result.get("filled"):
            logger.error("{} close unprotected open failed: {}", decision.symbol, result)
            await self._notifier.send(
                Event.ERROR,
                f"{decision.symbol} unprotected open close failed: {result}",
            )
            return
        pnl = realized_pnl(
            side=result.get("pos_side", ""),
            entry_price=result.get("entry_price", 0.0),
            exit_price=result.get("price", 0.0),
            qty=result.get("qty", 0.0),
        )
        self.runtime.add_realized_pnl(pnl)
        self.runtime.mark_order_event(decision.symbol)
        logger.error(
            "{} closed unprotected filled qty={} pnl={:.2f} reason={}",
            decision.symbol, result.get("qty"), pnl, reason,
        )
        await self._notifier.send(
            Event.CLOSE,
            f"{decision.symbol} closed unprotected filled qty={result.get('qty')} pnl={pnl:.2f}",
        )

    async def _disable_symbol_due_protection_failure(self, symbol: str, reason: str) -> None:
        await self._set_symbol_disabled(
            symbol,
            reason_code="PROTECTION_FAILURE",
            reason=reason,
            source="engine",
            action="disable_new_entries",
        )
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

    async def _disable_symbol_and_emergency_close(
        self,
        symbol: str,
        *,
        reason_code: str,
        reason: str,
        source: str,
    ) -> None:
        await self._set_symbol_disabled(
            symbol,
            reason_code=reason_code,
            reason=reason,
            source=source,
            action="emergency_close",
        )
        logger.error(
            "{} emergency close starting after symbol disable: reason_code={} reason={}",
            symbol, reason_code, reason,
        )
        await self._notifier.send(
            Event.ERROR,
            f"{symbol} disabled; emergency close starting: {reason[:180]}",
        )
        await self._emergency_close_unprotected_position(symbol, reason=reason)

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
        await self._set_symbol_disabled(
            symbol,
            reason_code="STALE_CONDITION_ORDERS",
            reason=f"stale condition orders remain: {details}",
            source="engine",
            action="disable_new_entries",
        )
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

    async def _sync_condition_history(self, symbol: str, active_orders: list[dict]) -> None:
        if not hasattr(self._client, "fetch_condition_orders"):
            return
        live_ids = {str(order.get("id") or "") for order in active_orders if order.get("id")}
        try:
            history_raw = await self._client.fetch_condition_orders(symbol, limit=30)
        except Exception as e:
            logger.warning("fetch condition history {} failed: {}", symbol, e)
            return
        history: list[dict] = []
        for raw in history_raw:
            order = normalize_condition_order(raw)
            if order["symbol"] != symbol or order["kind"] not in ("SL", "TP"):
                continue
            order["raw"] = raw
            history.append(order)
        if not history:
            return
        try:
            changed = await self._store.sync_condition_order_history(
                symbol=symbol,
                live_exchange_order_ids=live_ids,
                history_orders=history,
            )
            if changed:
                logger.info("synced {} condition order history rows for {}", changed, symbol)
        except Exception as e:
            logger.warning("sync condition history {} failed: {}", symbol, e)

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
            # 外部/手工条件单由系统观察和告警，但绝不自动撤销。
            if str(order.get("origin") or "EXTERNAL") != "ENGINE":
                continue
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
    def _protection_collection_alerts(
        orders: list[dict],
        *,
        qty: float,
    ) -> list[str]:
        qty_tol = max(abs(qty) * 1e-6, 1e-12)
        sl_orders = [order for order in orders if order.get("kind") == "SL"]
        tp_orders = [order for order in orders if order.get("kind") == "TP"]
        tp_qty = sum(
            qty if order.get("close_position") else max(0.0, float(order.get("qty") or 0.0))
            for order in tp_orders
        )
        alerts: list[str] = []
        if len(sl_orders) > 1:
            alerts.append(f"MULTIPLE_SL:{len(sl_orders)}")
        if tp_qty - qty > qty_tol:
            alerts.append(f"TP_OVER_COVERED:{tp_qty:g}>{qty:g}")
        elif tp_orders and qty - tp_qty > qty_tol:
            alerts.append(f"TP_PARTIAL_COVERAGE:{tp_qty:g}<{qty:g}")
        origins = {str(order.get("origin") or "EXTERNAL") for order in orders}
        if len(origins) > 1:
            alerts.append("MIXED_AUTHORITY")
        return alerts

    async def _report_protection_collection_alerts(
        self,
        symbol: str,
        orders: list[dict],
        *,
        qty: float,
    ) -> None:
        alerts = tuple(self._protection_collection_alerts(orders, qty=qty))
        previous = self._last_protection_alerts.get(symbol, ())
        if alerts == previous:
            return
        if not alerts:
            self._last_protection_alerts.pop(symbol, None)
            return
        self._last_protection_alerts[symbol] = alerts
        message = f"{symbol} protection collection alert: {', '.join(alerts)}"
        logger.warning(message)
        await self._store.log_audit(
            symbol=symbol,
            action="PROTECTION_ALERT",
            reason=message,
        )
        await self._notifier.send(Event.ERROR, message)

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
        if not order.get("reduce_only") and not order.get("close_position"):
            return "not reduceOnly"
        side = (order.get("side") or "").lower()
        if side and side != close_side:
            return f"side {side} != close side {close_side}"
        order_qty = float(order.get("qty") or 0.0)
        qty_tol = max(abs(qty) * 1e-6, 1e-12)
        if order.get("close_position"):
            if kind not in ("SL", "TP"):
                return "closePosition unsupported kind"
        elif kind == "SL":
            if order_qty <= 0 or abs(order_qty - qty) > qty_tol:
                return f"SL qty {order_qty:g} != position qty {qty:g}"
        elif order_qty <= 0 or order_qty - qty > qty_tol:
            return f"TP qty {order_qty:g} exceeds position qty {qty:g}"
        trigger = float(order.get("trigger_price") or 0.0)
        if trigger <= 0 or entry <= 0:
            return "invalid trigger/entry"
        # Active exchange protection may remain open while price is crossing its trigger.
        # Do not classify it as stale by current mark, or we can cancel the order that
        # should be allowed to trigger.
        # SL may be above entry (profit-lock / trailing stop via ADJUST_SLTP) — only
        # require it to be on the correct side of mark, not entry.
        if pos_side == "long":
            if kind == "SL" and not (trigger < mark):
                return f"long SL trigger {trigger:g} not below mark {mark:g}"
            if kind == "TP" and not (trigger > entry):
                return f"long TP trigger {trigger:g} not above entry"
        elif pos_side == "short":
            if kind == "SL" and not (trigger > mark):
                return f"short SL trigger {trigger:g} not above mark {mark:g}"
            if kind == "TP" and not (trigger < entry):
                return f"short TP trigger {trigger:g} not below entry"
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
        action = (latest_decision or {}).get("action", "")
        expected_action = "OPEN_LONG" if side == "long" else "OPEN_SHORT"
        decision_ts = int((latest_decision or {}).get("ts_ms") or 0)
        decision_trigger = 0.0
        if action == expected_action:
            pct_key = "stop_loss_pct" if kind == "SL" else "take_profit_pct"
            pct = float((latest_decision or {}).get(pct_key) or 0.0)
            if pct > 0:
                if kind == "SL":
                    decision_trigger = entry * (1 - pct) if side == "long" else entry * (1 + pct)
                else:
                    decision_trigger = entry * (1 + pct) if side == "long" else entry * (1 - pct)

        close_side = "sell" if side == "long" else "buy"
        template_trigger = 0.0
        template_ts = 0
        template_side = str((template or {}).get("side") or "").lower()
        if (
            template
            and template_side in ("", close_side)
            and float(template.get("price") or 0.0) > 0
        ):
            template_trigger = float(template["price"])
            template_ts = int(template.get("ts_ms") or 0)

        if template_trigger > 0 and (decision_trigger <= 0 or template_ts >= decision_ts):
            return template_trigger, "历史条件单"
        if decision_trigger > 0:
            logger.info(
                "repair {} {} trigger reconstructed from decision {}",
                symbol, kind, (latest_decision or {}).get("id"),
            )
            return decision_trigger, "最近开仓决策"
        if template_trigger > 0:
            return template_trigger, "历史条件单"
        return 0.0, ""

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
        leverage: int = 0,
    ) -> str:
        if trigger <= 0 or entry <= 0 or mark <= 0 or qty <= 0:
            return "价格或数量无效"
        if side == "long":
            # SL may be above entry (profit-lock via ADJUST_SLTP); only require it below mark.
            if kind == "SL" and not (trigger < mark):
                return f"多单止损必须低于当前标记价 {mark:.2f}"
            if kind == "TP" and not (trigger > mark and trigger > entry):
                return f"多单止盈必须高于当前标记价 {mark:.2f} 且高于开仓价 {entry:.2f}"
        elif side == "short":
            if kind == "SL" and not (trigger > mark):
                return f"空单止损必须高于当前标记价 {mark:.2f}"
            if kind == "TP" and not (trigger < mark and trigger < entry):
                return f"空单止盈必须低于当前标记价 {mark:.2f} 且低于开仓价 {entry:.2f}"
        else:
            return f"未知持仓方向 {side}"

        if kind == "SL":
            loss = (entry - trigger) * qty if side == "long" else (trigger - entry) * qty
            margin = entry * qty / max(leverage or self._settings.risk.max_leverage, 1)
            max_loss = margin * (
                self._settings.risk.max_loss_per_order_margin_pct / 100.0
            )
            if max_loss <= 0:
                return "无法计算订单保证金，不能校验止损风险"
            # loss < 0 means profit-lock (SL above entry for long / below entry for short).
            # That is a valid state from ADJUST_SLTP — skip the max_loss cap check since
            # hitting this SL would lock in profit, not incur a loss.
            if loss < 0:
                pass  # profit-lock SL — valid, no cap check needed
            elif loss > max_loss:
                return (
                    f"理论止损亏损 {loss:.2f} USDT 超过上限 {max_loss:.2f} USDT "
                    f"({self._settings.risk.max_loss_per_order_margin_pct}% of "
                    f"estimated order margin {margin:.2f})"
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
        elif (
            not rt.drawdown_bypass_active()
            and rt.risk_day_drawdown_pct >= risk.max_drawdown_pct
        ):
            breached = (
                f"daily drawdown {rt.risk_day_drawdown_pct:.2f}% "
                f">= {risk.max_drawdown_pct}%"
            )
        if breached and (
            not rt.halt_new_entries
            or not rt.halt_new_entries_reason.startswith("circuit breaker")
        ):
            logger.warning("CIRCUIT BREAKER: {}", breached)
            rt.trip_breaker(breached)
            reason_code = "DAILY_LOSS" if breached.startswith("daily loss") else "MAX_DRAWDOWN"
            await self._persist_strategy_pause_reason(
                reason_code=reason_code,
                reason=rt.halt_new_entries_reason,
                source="engine:circuit_breaker",
            )
            flatten_error: Exception | None = None
            try:
                results = await self._executor.flatten_all(symbols=self._tracked_symbols())
                for result in results:
                    await self._store.log_order(result)
                flattened = sum(1 for result in results if result.get("filled"))
            except Exception as e:
                logger.error("circuit-breaker flatten failed: {}", e)
                flatten_error = e
                flattened = 0
                await self._store.record_system_command(
                    "CIRCUIT_BREAKER",
                    arg=reason_code,
                    source="engine",
                    status="failed",
                    result=f"{rt.halt_new_entries_reason}; flatten failed: {e}",
                )
            else:
                await self._store.record_system_command(
                    "CIRCUIT_BREAKER",
                    arg=reason_code,
                    source="engine",
                    status="done",
                    result=f"{rt.halt_new_entries_reason}; flattened {flattened} positions",
                )
            await self._notifier.send(Event.CIRCUIT_BREAK, breached)
            if flatten_error is not None:
                raise RuntimeError(
                    f"circuit breaker tripped but emergency flatten failed: {flatten_error}"
                ) from flatten_error
            return True
        return rt.halt_new_entries

    async def _enrich_position_timing(self, symbol: str, position: PositionSnapshot) -> None:
        """Add local lifecycle timing to the LLM-only position snapshot."""
        now_ms = int(time.time() * 1000)
        trade = await self._store.latest_open_trade_summary(symbol)
        if trade:
            opened_at_ms = int(trade.get("opened_at_ms") or 0)
            if opened_at_ms > 0:
                age_minutes = max(0.0, (now_ms - opened_at_ms) / 60_000.0)
                position.opened_at_ms = opened_at_ms
                position.position_age_minutes = round(age_minutes, 2)
                position.position_age_1m_bars = int(age_minutes)

        sltp_state = self._last_sltp_adjust.get(symbol)
        if sltp_state and sltp_state.ts_ms > 0:
            position.last_sltp_adjust_at_ms = int(sltp_state.ts_ms)
            position.minutes_since_last_sltp_adjust = round(
                max(0.0, (now_ms - sltp_state.ts_ms) / 60_000.0),
                2,
            )

        close_state = self._close_confirmations.get(symbol)
        if close_state:
            position.close_confirm_count = int(close_state.count or 0)

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
        if position.has_position:
            await self._enrich_position_timing(symbol, position)
            try:
                active_orders = await self._active_protection_orders(symbol)
                origins: set[str] = set()
                tp_covered_qty = 0.0
                for index, order in enumerate(active_orders, start=1):
                    origin = str(order.get("origin") or "EXTERNAL")
                    origins.add(origin)
                    snapshot = ProtectionOrderSnapshot(
                        kind=str(order.get("kind") or ""),
                        trigger_price=float(order.get("trigger_price") or 0.0),
                        qty=float(order.get("qty") or 0.0),
                        close_position=bool(order.get("close_position")),
                        origin=origin,
                        leg_id=str(order.get("leg_id") or ""),
                    )
                    if order["kind"] == "SL" and order.get("trigger_price"):
                        position.sl_price = float(order["trigger_price"])
                        position.sl_orders.append(snapshot)
                    elif order["kind"] == "TP" and order.get("trigger_price"):
                        if position.tp_price is None:
                            position.tp_price = float(order["trigger_price"])
                        if not snapshot.leg_id:
                            snapshot.leg_id = f"TP{len(position.tp_orders) + 1}"
                        position.tp_orders.append(snapshot)
                        tp_covered_qty += (
                            float(position.size or 0.0)
                            if snapshot.close_position
                            else snapshot.qty
                        )
                position.protection_authority = (
                    next(iter(origins)) if len(origins) == 1
                    else "MIXED" if origins
                    else "NONE"
                )
                position.tp_orders.sort(key=lambda row: row.trigger_price)
                position.sl_orders.sort(key=lambda row: row.trigger_price)
                position.tp_coverage_pct = (
                    min(tp_covered_qty / float(position.size or 0.0), 1.0)
                    if float(position.size or 0.0) > 0 else 0.0
                )
                position.runner_qty = max(
                    0.0, float(position.size or 0.0) - tp_covered_qty
                )
            except Exception as _e:
                logger.debug("fetch protection orders for snapshot failed {}: {}", symbol, _e)
        higher_tf = await self._fetch_higher_tf_safe(symbol)
        micro_klines = await self._fetch_micro_klines_safe(symbol)
        leader_snapshot = await self._get_cycle_leader_snapshot(symbol)
        current_feature_snapshot = build_feature_snapshot(
            symbol=symbol,
            snapshot=snap,
            position=position,
            higher_tf_klines=higher_tf,
            micro_klines=micro_klines,
            leader_snapshot=leader_snapshot,
        )
        last_feature_snapshot = None
        raw_last_snapshot = self.runtime.last_decision_snapshot.get(symbol)
        if raw_last_snapshot:
            try:
                last_feature_snapshot = FeatureSnapshot.model_validate(raw_last_snapshot)
            except Exception:
                last_feature_snapshot = None

        # 1. 节流：是否调用 LLM
        engine_settings = self._engine_settings
        gate = should_call_llm(
            symbol=symbol,
            last_price=snap.last_price,
            last_decision_px=self.runtime.last_decision_price.get(symbol),
            position=position,
            price_change_pct=engine_settings.price_change_pct,
            pnl_alert_pct=engine_settings.pnl_alert_pct,
            order_event=self.runtime.pop_order_event(symbol),
            trigger_on_order_event=engine_settings.trigger_on_order_event,
            skip_count=self.runtime.skip_count.get(symbol, 0),
            max_skip_cycles=engine_settings.max_skip_cycles,
            last_decision_ts_ms=self.runtime.last_decision_time.get(symbol),
            now_ts_ms=snap.updated_ms or int(time.time() * 1000),
            current_snapshot=current_feature_snapshot,
            last_decision_snapshot=last_feature_snapshot,
            feature_snapshot_enabled=engine_settings.feature_snapshot_enabled,
            ema_spread_cross_min_pct=engine_settings.ema_spread_cross_min_pct,
            macd_hist_cross_min_abs=engine_settings.macd_hist_cross_min_abs,
            rsi_midline=engine_settings.rsi_midline,
            boll_bandwidth_low_pct=engine_settings.boll_bandwidth_low_pct,
            boll_bandwidth_expand_pct=engine_settings.boll_bandwidth_expand_pct,
            volume_zscore_trigger=engine_settings.volume_zscore_trigger,
            micro_return_5m_trigger_pct=engine_settings.micro_return_5m_trigger_pct,
            micro_range_5m_trigger_pct=engine_settings.micro_range_5m_trigger_pct,
            near_exit_pnl_pct=engine_settings.near_exit_pnl_pct,
            review_flat_seconds=engine_settings.review_flat_seconds,
            review_position_seconds=engine_settings.review_position_seconds,
            review_near_exit_seconds=engine_settings.review_near_exit_seconds,
            review_high_vol_seconds=engine_settings.review_high_vol_seconds,
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
        ctx = build_context(
            symbol=symbol, snapshot=snap, position=position,
            available_margin=margin, settings=self._settings,
            equity=self.runtime.current_equity,
            higher_tf_klines=higher_tf,
            micro_klines=micro_klines,
        )
        if ctx is None:
            logger.warning("context unavailable for {}, skip", symbol)
            return

        # 调用 LLM 期间持有锁；热替换会等待当前决策完成。
        async with self._llm_lock:
            decision, llm_trace = await self._llm.decide_with_trace(ctx)
        feature_snapshot_json = (
            current_feature_snapshot.model_dump_json() if current_feature_snapshot is not None else ""
        )
        self.runtime.record_decision(
            symbol,
            snap.last_price,
            feature_snapshot=(
                current_feature_snapshot.model_dump(mode="json")
                if current_feature_snapshot is not None else None
            ),
        )
        await self._store.log_decision(
            symbol=symbol,
            decision=decision,
            ctx=ctx,
            skipped=False,
            ref_price=snap.last_price,
            llm_system_prompt=getattr(llm_trace, "system_prompt", ""),
            llm_prompt=llm_trace.user_prompt,
            llm_request_json=llm_trace.request_json,
            llm_response_json=llm_trace.response_json,
            feature_snapshot_json=feature_snapshot_json,
            llm_latency_ms=llm_trace.latency_ms,
            llm_attempts=llm_trace.attempts,
            llm_status=llm_trace.status,
            llm_error=llm_trace.error,
        )

        # CLOSE 优先处理（不受开仓限额约束）
        if decision.action == Action.CLOSE:
            if not await self._allow_strategy_close(decision, ctx):
                return
            await self._handle_close(symbol)
            return
        if decision.action == Action.HOLD:
            return
        if decision.action == Action.ADJUST_SLTP:
            await self._handle_adjust_sltp(decision, ctx)
            return

        # 3. 风控逐项校验
        await self._handle_open(decision, ctx)

    async def _handle_open(self, decision: TradeDecision, ctx: MarketContext) -> None:
        symbol = decision.symbol
        snap = self._market.snapshot(symbol)
        if not snap.fresh:
            verdict = Verdict.reject(
                RejectCode.STALE_MARKET_DATA,
                "market ticker/klines refresh incomplete; new entry blocked",
            )
            await self._store.log_reject(symbol=symbol, verdict=verdict, decision=decision)
            return
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
            halt_new_entries_reason=self.runtime.halt_new_entries_reason,
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

        # 4. 执行（精度规整在 executor 内）。先声明 ownership，避免 maker
        # 部分成交早于本地 trade 落库时被周期对账误判成外部持仓。
        claim_id = await self._store.begin_position_claim(
            symbol=symbol,
            side="long" if decision.action == Action.OPEN_LONG else "short",
            planned_qty=verdict.qty,
            ttl_ms=_ENTRY_CLAIM_TTL_MS,
            reason="strategy open",
        )
        try:
            result = await self._executor.open_position(
                decision=decision, qty=verdict.qty, price=ctx.last_price
            )
            logged = await self._store.log_order(result)
            claim_status = (
                "protecting" if result.get("filled")
                else str(result.get("status") or "unknown")
            )
            await self._store.finish_position_claim(
                claim_id,
                status=claim_status,
                filled_qty=float(result.get("qty") or 0.0) if result.get("filled") else 0.0,
                entry_price=float(result.get("price") or 0.0),
                client_order_id=str(result.get("client_order_id") or ""),
                raw=result.get("raw") if isinstance(result.get("raw"), dict) else None,
            )
        except Exception as e:
            await self._store.finish_position_claim(
                claim_id, status="error", reason=str(e)[:240]
            )
            raise
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
                # C2：下 SL/TP 之前用交易所侧最新持仓 + 现存条件单做二次确认。
                # 防止 race：本地 order 已成但 entry_price / qty 与交易所侧略有不一致
                # （均价计算方式差异、cumulative qty 漂移等），或重复挂保护单。
                try:
                    precheck = await self._precheck_before_attach_sl_tp(
                        symbol=symbol,
                        decision=decision,
                        open_result=result,
                    )
                except Exception as e:
                    logger.warning("{} pre-attach sl/tp check failed: {}; fall through", symbol, e)
                    precheck = {"ok": True, "qty": float(result["qty"]), "entry": float(result["price"])}
                if not precheck.get("ok"):
                    failure_reason = f"pre-attach sl/tp check failed: {precheck.get('reason', '')}"
                    await self._handle_unprotected_open_failure(
                        decision=decision,
                        open_result=result,
                        reason=failure_reason,
                    )
                    await self._store.finish_position_claim(
                        claim_id,
                        status="error",
                        filled_qty=float(result.get("qty") or 0.0),
                        entry_price=float(result.get("price") or 0.0),
                        client_order_id=str(result.get("client_order_id") or ""),
                        reason=failure_reason[:240],
                        raw=result.get("raw") if isinstance(result.get("raw"), dict) else None,
                    )
                    return
                attach_qty = float(precheck.get("qty") or result["qty"])
                attach_entry = float(precheck.get("entry") or result["price"])
                specs = self._planned_protection_specs(decision, attach_entry, attach_qty)
                reject_reason = self._protection_specs_reject_reason(
                    symbol=symbol,
                    qty=attach_qty,
                    specs=specs,
                )
                if reject_reason:
                    failure_reason = f"protection order below exchange minimum: {reject_reason}"
                    await self._handle_unprotected_open_failure(
                        decision=decision,
                        open_result=result,
                        reason=failure_reason,
                    )
                    await self._store.finish_position_claim(
                        claim_id,
                        status="error",
                        filled_qty=float(result.get("qty") or 0.0),
                        entry_price=float(result.get("price") or 0.0),
                        client_order_id=str(result.get("client_order_id") or ""),
                        reason=failure_reason[:240],
                        raw=result.get("raw") if isinstance(result.get("raw"), dict) else None,
                    )
                    return
                sltp = await self._executor.place_sl_tp(
                    decision=decision, entry_price=attach_entry, qty=attach_qty
                )
                trade_id = int((logged or {}).get("trade_id") or 0)
                for o in sltp:
                    if trade_id > 0:
                        o["trade_id"] = trade_id
                    if decision.leverage > 0:
                        o["leverage"] = decision.leverage
                        o["margin"] = float(o.get("notional") or 0.0) / decision.leverage
                    await self._store.log_order(o)
                # 用 attach_qty 校正 open_result 给后续缺单检查用
                attach_result = dict(result)
                attach_result["qty"] = attach_qty
                attach_result["price"] = attach_entry
                missing_stop = (
                    decision.stop_loss_pct > 0
                    and not any(
                        order.get("kind") == "SL" and order.get("status") == "placed"
                        for order in sltp
                    )
                )
                await self._handle_missing_stop_after_open(
                    decision=decision,
                    open_result=attach_result,
                    protection_orders=sltp,
                )
                if missing_stop:
                    await self._store.finish_position_claim(
                        claim_id,
                        status="error",
                        filled_qty=float(result.get("qty") or 0.0),
                        entry_price=float(result.get("price") or 0.0),
                        client_order_id=str(result.get("client_order_id") or ""),
                        reason="SL protection was not confirmed after open",
                        raw=result.get("raw") if isinstance(result.get("raw"), dict) else None,
                    )
                    return
            await self._store.finish_position_claim(
                claim_id,
                status=str(result.get("status") or "filled"),
                filled_qty=float(result.get("qty") or 0.0),
                entry_price=float(result.get("price") or 0.0),
                client_order_id=str(result.get("client_order_id") or ""),
                raw=result.get("raw") if isinstance(result.get("raw"), dict) else None,
            )
            await self._refresh_positions_after_open()

    async def _refresh_positions_after_open(self) -> None:
        """Refresh account exposure before another symbol can open in this cycle."""
        positions = await self._fetch_positions_safe()
        if positions is None:
            self.runtime.halt_entries("post-open position query failed")
            await self._persist_strategy_pause_reason(
                reason_code="POST_OPEN_POSITION_QUERY_FAILED",
                reason=self.runtime.halt_new_entries_reason,
                source="engine:post_open",
            )
            return
        self.runtime.positions = {
            normalize_symbol(p.get("symbol")): p
            for p in positions
            if float(p.get("contracts") or 0.0) != 0
        }

    async def _allow_strategy_close(self, decision: TradeDecision, ctx: MarketContext) -> bool:
        """Hard gate for LLM-requested active CLOSE decisions."""
        symbol = decision.symbol
        raw = self.runtime.positions.get(symbol)
        if not raw:
            return True
        trade = await self._store.latest_open_trade_summary(symbol)
        if not trade:
            return True
        side = str(raw.get("side") or "").lower()
        entry = float(raw.get("entryPrice") or raw.get("entry_price") or trade.get("entry_price") or 0.0)
        mark = await self._current_mark_price(symbol, raw)
        now_ms = int(time.time() * 1000)
        settings = self._engine_settings
        result = evaluate_close_guard(
            state=self._close_confirmations.get(symbol),
            trade_id=int(trade["id"]),
            opened_at_ms=int(trade["opened_at_ms"] or 0),
            now_ms=now_ms,
            side=side,
            entry_price=entry,
            mark_price=mark,
            atr=float(ctx.indicators.atr or 0.0),
            min_age_seconds=int(settings.close_confirm_min_1m_bars) * 60,
            min_confirmations=int(settings.close_confirm_min_count),
            loss_atr_multiple=float(settings.close_block_loss_atr_multiple),
            confirmation_window_seconds=int(settings.close_confirm_window_seconds),
        )
        self._close_confirmations[symbol] = result.state
        if result.allowed:
            self._close_confirmations.pop(symbol, None)
            return True
        await self._store.log_audit(
            symbol=symbol,
            action="CLOSE_BLOCKED",
            reason=(
                f"LLM CLOSE blocked by strategy guard: {result.reason}; "
                f"trade_id={trade['id']} entry={entry:.8g} mark={mark:.8g} "
                f"atr={float(ctx.indicators.atr or 0.0):.8g}"
            ),
        )
        logger.warning("[{}] LLM CLOSE blocked: {}", symbol, result.reason)
        return False

    async def _handle_close(self, symbol: str) -> None:
        raw = self.runtime.positions.get(symbol)
        if not raw:
            logger.info("[{}] CLOSE requested but no position", symbol)
            return
        result = await self._executor.close_position(
            raw,
            mode=self._settings.execution.normal_exit_mode,
        )
        if result.get("filled") and result.get("status") != "partial":
            self._mark_recent_explicit_close(symbol)
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
            self._close_confirmations.pop(symbol, None)
            self._last_sltp_adjust.pop(symbol, None)
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

    async def _handle_adjust_sltp(self, decision: "TradeDecision", ctx: "MarketContext") -> None:
        """LLM 决策 ADJUST_SLTP：撤旧条件单、按标记价和新 pct 重挂。不平仓。"""
        symbol = decision.symbol
        position = await self._fetch_exchange_position(symbol)
        if position is None:
            logger.info("[{}] ADJUST_SLTP: 交易所无持仓，忽略", symbol)
            return

        side = position["side"]
        qty = float(position["contracts"] or 0.0)
        entry = float(position["entry_price"] or 0.0)
        mark = await self._current_mark_price(symbol, position)
        if side not in ("long", "short") or qty <= 0 or entry <= 0 or mark <= 0:
            logger.warning("[{}] ADJUST_SLTP: 持仓数据不完整，跳过", symbol)
            return

        is_long = side == "long"
        filters = self._client.filters(symbol)
        equity = await self._current_equity()
        trade = await self._store.latest_open_trade_summary(symbol)
        trade_id = int((trade or {}).get("id") or 0)
        old_orders = await self._active_protection_orders(symbol)
        external_orders = [
            order for order in old_orders
            if str(order.get("origin") or "EXTERNAL") != "ENGINE"
        ]
        if external_orders:
            await self._store.log_audit(
                symbol=symbol,
                action="SLTP_BLOCKED",
                reason=(
                    "LLM ADJUST_SLTP blocked because protection authority is "
                    f"{'MIXED' if len(external_orders) < len(old_orders) else 'EXTERNAL'}; "
                    "explicit PROTECT_POSITION takeover is required"
                ),
            )
            logger.warning(
                "[{}] ADJUST_SLTP blocked: external protection orders require explicit takeover",
                symbol,
            )
            return
        old_sl = next(
            (
                float(order.get("trigger_price") or order.get("price") or 0.0)
                for order in old_orders
                if order.get("kind") == "SL"
            ),
            0.0,
        )

        # 用标记价作为基准计算新触发价
        specs: list[ProtectionOrderSpec] = []
        rejected: list[str] = []
        requested: list[ProtectionOrderSpec] = []
        if decision.stop_loss_pct > 0:
            requested.append(ProtectionOrderSpec(
                "SL", "STOP_MARKET", decision.stop_loss_pct,
                qty=qty, leg_id="SL",
            ))
        allocated_qty = 0.0
        targets = decision.effective_take_profit_targets
        for index, target in enumerate(targets, start=1):
            is_last = index == len(targets)
            if is_last and sum(item.position_pct for item in targets) >= 1.0 - 1e-9:
                target_qty = max(0.0, qty - allocated_qty)
            else:
                from src.exchange.filters import round_qty
                target_qty = float(round_qty(qty * target.position_pct, filters))
            allocated_qty += target_qty
            requested.append(ProtectionOrderSpec(
                "TP", "TAKE_PROFIT_MARKET", target.price_distance_pct,
                qty=target_qty,
                leg_id=target.leg_id or f"TP{index}",
                position_pct=target.position_pct,
            ))

        for requested_spec in requested:
            kind = requested_spec.kind
            pct = requested_spec.trigger_price
            if pct <= 0:
                continue
            raw_trigger = (
                mark * (1 - pct) if (kind == "SL") == is_long else mark * (1 + pct)
            )
            trigger = float(round_price(raw_trigger, filters))
            reason = self._validate_adjust_trigger(
                symbol=symbol, side=side, kind=kind,
                trigger=trigger, entry=entry, mark=mark,
                qty=qty, equity=equity, leverage=int(position.get("leverage") or 0),
            )
            if reason:
                rejected.append(f"{requested_spec.leg_id or kind}@{trigger:.4f}: {reason}")
                continue
            specs.append(ProtectionOrderSpec(
                kind=kind,
                order_type=requested_spec.order_type,
                trigger_price=trigger,
                qty=requested_spec.qty,
                leg_id=requested_spec.leg_id,
                position_pct=requested_spec.position_pct,
            ))

        if not specs:
            logger.warning("[{}] ADJUST_SLTP: 全部触发价校验失败 {}", symbol, rejected)
            return
        new_sl = next(
            (spec.trigger_price for spec in specs if spec.kind == "SL"),
            0.0,
        )
        guard = evaluate_sltp_adjust_guard(
            state=self._last_sltp_adjust.get(symbol),
            trade_id=trade_id,
            now_ms=int(time.time() * 1000),
            side=side,
            entry_price=entry,
            mark_price=mark,
            old_sl=old_sl,
            new_sl=new_sl,
            atr=float(ctx.indicators.atr or 0.0),
            min_interval_seconds=int(self._engine_settings.sltp_adjust_min_seconds),
            min_improve_atr_multiple=float(self._engine_settings.sltp_adjust_min_atr_multiple),
            breakeven_buffer_pct=float(self._engine_settings.breakeven_fee_slippage_buffer_pct),
        )
        if not guard.allowed:
            await self._store.log_audit(
                symbol=symbol,
                action="SLTP_BLOCKED",
                reason=(
                    f"LLM ADJUST_SLTP blocked by strategy guard: {guard.reason}; "
                    f"trade_id={trade_id} side={side} entry={entry:.8g} mark={mark:.8g} "
                    f"old_sl={old_sl:.8g} new_sl={new_sl:.8g} atr={float(ctx.indicators.atr or 0.0):.8g}"
                ),
            )
            logger.warning("[{}] ADJUST_SLTP blocked: {}", symbol, guard.reason)
            return

        # 先放置并确认新保护单，再逐个撤销被替换的旧单，避免出现裸仓窗口。
        results = await self._executor.place_protection_orders(
            symbol=symbol, pos_side=side, qty=qty, specs=specs,
        )
        for order in results:
            await self._store.log_order(order)

        placed = [f"{o['kind']}@{float(o.get('price') or o.get('trigger_price') or 0):.4f}"
                  for o in results if o.get("status") == "placed"]
        failed = [f"{o.get('kind')}:{(o.get('raw') or {}).get('error') or o.get('status')}"
                  for o in results if o.get("status") != "placed"]

        self.runtime.mark_order_event(symbol)
        logger.warning(
            "ADJUST_SLTP {} side={} mark={} placed={} failed={} rejected={}",
            symbol, side, mark, placed, failed, rejected,
        )

        # SL 挂单失败 → 持仓无保护，禁用该标的并尝试紧急平仓
        sl_failed = decision.stop_loss_pct > 0 and not any(
            o.get("kind") == "SL" and o.get("status") == "placed" for o in results
        )
        if sl_failed:
            await self._disable_symbol_due_protection_failure(
                symbol, f"ADJUST_SLTP SL place failed: {failed}"
            )
            await self._emergency_close_unprotected_position(
                symbol, reason="ADJUST_SLTP SL not placed"
            )
            return

        replaced_kinds = {
            o.get("kind") for o in results if o.get("status") == "placed"
        }
        stale_old = [
            o for o in old_orders
            if o.get("kind") in replaced_kinds
            and str(o.get("origin") or "EXTERNAL") == "ENGINE"
        ]
        remaining = await self._cancel_stale_condition_orders(
            symbol=symbol, orders=stale_old, reason="adjust_sltp_replace"
        )
        if remaining:
            details = ", ".join(self._condition_order_label(o) for o in remaining)
            logger.warning("[{}] ADJUST_SLTP: replaced old conditions remain: {}", symbol, details)
        if new_sl > 0:
            self._last_sltp_adjust[symbol] = SltpAdjustState(
                trade_id=trade_id,
                ts_ms=int(time.time() * 1000),
                sl_price=new_sl,
            )

    def _validate_adjust_trigger(
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
        leverage: int = 0,
    ) -> str:
        """校验 ADJUST_SLTP 触发价。基准是 mark（允许止损移至盈利侧）。"""
        if trigger <= 0 or mark <= 0 or qty <= 0:
            return "价格或数量无效"
        if side == "long":
            if kind == "SL" and trigger >= mark:
                return f"多单止损必须低于当前标记价 {mark:.4f}"
            if kind == "TP" and trigger <= mark:
                return f"多单止盈必须高于当前标记价 {mark:.4f}"
        elif side == "short":
            if kind == "SL" and trigger <= mark:
                return f"空单止损必须高于当前标记价 {mark:.4f}"
            if kind == "TP" and trigger >= mark:
                return f"空单止盈必须低于当前标记价 {mark:.4f}"
        else:
            return f"未知持仓方向 {side}"

        # SL：理论亏损上限校验（以 entry 为基准计算实际亏损）
        if kind == "SL" and equity > 0:
            loss = (entry - trigger) * qty if side == "long" else (trigger - entry) * qty
            if loss < 0:
                # 止损在盈利侧（loss<0 表示锁利），直接通过
                pass
            else:
                margin = entry * qty / max(leverage or self._settings.risk.max_leverage, 1)
                max_loss = margin * (
                    self._settings.risk.max_loss_per_order_margin_pct / 100.0
                )
                if max_loss > 0 and loss > max_loss:
                    return (
                        f"理论止损亏损 {loss:.2f} USDT 超过上限 {max_loss:.2f} USDT "
                        f"({self._settings.risk.max_loss_per_order_margin_pct}% of "
                        f"estimated order margin {margin:.2f})"
                    )
        return ""


    def _mark_recent_explicit_close(self, symbol: str) -> None:
        self._recent_explicit_closes[normalize_symbol(symbol)] = (
            time.monotonic() + _POST_CLOSE_RECONCILE_GRACE_SECONDS
        )

    def _has_recent_explicit_close(self, symbol: str) -> bool:
        symbol = normalize_symbol(symbol)
        deadline = self._recent_explicit_closes.get(symbol)
        if deadline is None:
            return False
        if time.monotonic() <= deadline:
            return True
        self._recent_explicit_closes.pop(symbol, None)
        return False

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

    async def _fetch_micro_klines_safe(self, symbol: str) -> list[list[float]]:
        """拉取短周期原始 K 线，供 Prompt 观察最近入场节奏。失败不阻塞主决策。"""
        limit = self._settings.llm.micro_kline_lookback
        if limit <= 0:
            return []
        tf = self._settings.llm.micro_kline_interval
        try:
            return await self._client.fetch_ohlcv(symbol, tf, limit)
        except Exception as e:
            logger.warning("fetch micro klines {} {} failed: {}", tf, symbol, e)
            return []

    async def _get_cycle_leader_snapshot(self, symbol: str) -> FeatureSnapshot | None:
        """Return BTC feature snapshot for cross-symbol trigger checks."""
        leader = "BTCUSDT"
        if symbol == leader or leader not in self._tracked_symbols():
            return None
        if self._cycle_leader_snapshot is not None:
            return self._cycle_leader_snapshot
        try:
            snap = self._market.snapshot(leader)
            position = build_position_snapshot(self.runtime.positions.get(leader))
            higher_tf = await self._fetch_higher_tf_safe(leader)
            micro_klines = await self._fetch_micro_klines_safe(leader)
            self._cycle_leader_snapshot = build_feature_snapshot(
                symbol=leader,
                snapshot=snap,
                position=position,
                higher_tf_klines=higher_tf,
                micro_klines=micro_klines,
            )
        except Exception as e:
            logger.warning("build leader feature snapshot failed {}: {}", leader, e)
            self._cycle_leader_snapshot = None
        return self._cycle_leader_snapshot

    async def _fetch_positions_safe(self) -> list[dict] | None:
        try:
            return await self._client.fetch_positions(self._tracked_symbols())
        except Exception as e:
            logger.warning("fetch positions failed: {}", e)
            return None

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
                self._settings.user_stream.reconcile_active_seconds
                if self.runtime.positions
                else self._settings.user_stream.reconcile_idle_seconds
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
        async with self._state_sync_lock:
            await self._rest_resync_with_event_buffer(reason)
        await self._sync_exchange_fills()
        await self._enforce_exchange_invariants(reason)

    async def _sync_exchange_fills(self, *, force: bool = False) -> None:
        """REST compensation for private-stream fill gaps."""
        now = time.monotonic()
        if not force and now - self._last_fill_sync_at < 60.0:
            return
        self._last_fill_sync_at = now
        fallback_since = int(time.time() * 1000) - 300_000
        for symbol in self._tracked_symbols():
            try:
                watermark = await self._store.exchange_fill_watermark(symbol)
                since = max(0, watermark - 60_000) if watermark else fallback_since
                while True:
                    trades = await self._client.fetch_my_trades(symbol, since=since, limit=1000)
                    if not trades:
                        break
                    max_ts = since
                    for trade in trades:
                        fill = ccxt_trade_fill(trade, symbol)
                        if fill is None:
                            continue
                        position = self.runtime.positions.get(symbol) or {}
                        fill["leverage"] = int(position.get("leverage") or 0)
                        await self._store.ingest_exchange_fill(fill)
                        max_ts = max(max_ts, int(fill.get("ts_ms") or 0))
                    if len(trades) < 1000 or max_ts <= since:
                        break
                    since = max_ts + 1
            except Exception as e:
                logger.warning("{} exchange fill REST compensation failed: {}", symbol, e)
        try:
            resolved = await self._store.resolve_unknown_exchange_fills()
            if resolved:
                logger.info("reclassified {} deferred Binance fills", resolved)
        except Exception as e:
            logger.warning("deferred Binance fill reclassification failed: {}", e)

    async def _sync_open_orders_snapshot(self) -> None:
        orders = await self._fetch_open_orders_safe()
        if not self._account.started:
            self.runtime.open_orders = {}
            for order in orders:
                self.runtime.open_orders.setdefault(
                    normalize_symbol(order.get("symbol")), []
                ).append(order)
        if orders:
            await self._store.snapshot_open_orders(orders)

    async def _confirm_exchange_flat(self, symbol: str, reason: str) -> tuple[bool, dict | None]:
        """Confirm a missing exchange position before closing local state as EXCHANGE_FLAT."""
        attempts = len(_EXCHANGE_FLAT_CONFIRM_DELAYS_SECONDS)
        saw_fetch_error = False
        for attempt, delay in enumerate(_EXCHANGE_FLAT_CONFIRM_DELAYS_SECONDS, start=1):
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                raw_positions = await self._client.fetch_positions([symbol])
            except Exception as e:
                saw_fetch_error = True
                logger.warning(
                    "{} exchange-flat confirm attempt {}/{} failed during {}: {}",
                    symbol, attempt, attempts, reason, e,
                )
                continue
            for raw in raw_positions:
                position = normalize_position(raw)
                if position["symbol"] == symbol and position["contracts"] > 0:
                    self.runtime.positions[symbol] = position
                    logger.warning(
                        "{} exchange-flat reconcile deferred during {}; "
                        "position reappeared after {}/{} confirmations",
                        symbol, reason, attempt, attempts,
                    )
                    return False, position
        if saw_fetch_error:
            logger.warning(
                "{} exchange-flat confirm inconclusive during {}; defer local flat reconcile",
                symbol, reason,
            )
            return False, None
        return True, None

    async def _confirm_unmanaged_live_position(
        self,
        symbol: str,
        position: dict | None,
        reason: str,
    ) -> tuple[bool, dict | None]:
        """Confirm an unmanaged live position before disabling the symbol."""
        attempts = len(_UNMANAGED_LIVE_CONFIRM_DELAYS_SECONDS)
        min_confirmations = min(2, attempts)
        expected_side = (position or {}).get("side") or ""
        saw_fetch_error = False
        confirmations = 0
        latest_position: dict | None = None
        for attempt, delay in enumerate(_UNMANAGED_LIVE_CONFIRM_DELAYS_SECONDS, start=1):
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                raw_positions = await self._client.fetch_positions([symbol])
            except Exception as e:
                saw_fetch_error = True
                logger.warning(
                    "{} unmanaged-live confirm attempt {}/{} failed during {}: {}",
                    symbol, attempt, attempts, reason, e,
                )
                continue
            matched = None
            for raw in raw_positions:
                current = normalize_position(raw)
                if current["symbol"] != symbol or current["contracts"] <= 0:
                    continue
                if expected_side and current.get("side") != expected_side:
                    logger.warning(
                        "{} unmanaged-live confirm deferred during {}; side changed {} -> {}",
                        symbol, reason, expected_side, current.get("side") or "",
                    )
                    self.runtime.positions[symbol] = current
                    return False, current
                matched = current
                break
            if matched is None:
                self.runtime.positions.pop(symbol, None)
                logger.warning(
                    "{} unmanaged-live confirm deferred during {}; position disappeared "
                    "after {}/{} confirmations",
                    symbol, reason, attempt, attempts,
                )
                return False, None
            confirmations += 1
            latest_position = matched
            self.runtime.positions[symbol] = matched

        if confirmations >= min_confirmations and latest_position is not None:
            return True, latest_position
        if saw_fetch_error:
            logger.warning(
                "{} unmanaged-live confirm inconclusive during {}; defer symbol disable",
                symbol, reason,
            )
        return False, latest_position

    async def _exchange_flat_defer_reason(self, symbol: str, reason: str) -> str:
        """Return a non-empty reason when local flat reconciliation is unsafe."""
        try:
            if await self._store.has_recent_entry_claim(symbol):
                return "recent entry claim still inside TTL"
        except Exception as e:
            logger.warning(
                "{} exchange-flat recent claim guard failed during {}: {}",
                symbol, reason, e,
            )
            return "recent entry claim guard failed"
        try:
            if await self._store.has_fresh_open_trade(symbol, _EXCHANGE_FLAT_MIN_OPEN_AGE_MS):
                return f"fresh local open trade <{_EXCHANGE_FLAT_MIN_OPEN_AGE_MS}ms"
        except Exception as e:
            logger.warning(
                "{} exchange-flat fresh trade guard failed during {}: {}",
                symbol, reason, e,
            )
            return "fresh local open trade guard failed"
        return ""

    async def _enforce_exchange_invariants(self, reason: str) -> None:
        positions = {}
        for raw in await self._client.fetch_positions(self._tracked_symbols()):
            position = normalize_position(raw)
            if position["contracts"] > 0:
                positions[position["symbol"]] = position
        for symbol in self._tracked_symbols():
            try:
                active_orders = await self._active_protection_orders(symbol)
            except Exception as e:
                logger.warning("reconcile {} active protection query failed: {}", symbol, e)
                continue
            position = positions.get(symbol)
            if position is None:
                flat_checked_at_ms = int(time.time() * 1000)
                has_local_open = False
                try:
                    has_local_open = await self._store.has_open_trade(symbol)
                except Exception as e:
                    has_local_open = True
                    logger.warning(
                        "{} local open trade check failed during {}; confirm exchange flat first: {}",
                        symbol, reason, e,
                    )
                if not active_orders and not has_local_open:
                    continue

                defer_reason = await self._exchange_flat_defer_reason(symbol, reason)
                if defer_reason:
                    logger.warning(
                        "{} exchange-flat reconcile deferred during {}: {}",
                        symbol, reason, defer_reason,
                    )
                    continue

                flat_confirmed, live_position = await self._confirm_exchange_flat(symbol, reason)
                if live_position is not None:
                    position = live_position
                elif not flat_confirmed:
                    continue
                if position is None:
                    defer_reason = await self._exchange_flat_defer_reason(symbol, reason)
                    if defer_reason:
                        logger.warning(
                            "{} exchange-flat reconcile deferred after confirm during {}: {}",
                            symbol, reason, defer_reason,
                        )
                        continue
                    await self._sync_condition_history(symbol, active_orders)
                    live_ids = {str(order.get("id") or "") for order in active_orders}
                    await self._store.mark_symbol_conditions_not_live(symbol, live_ids)
                    closed = await self._store.reconcile_symbol_flat(
                        symbol,
                        reason="EXCHANGE_FLAT",
                        opened_before_ms=flat_checked_at_ms,
                        min_open_age_ms=_EXCHANGE_FLAT_MIN_OPEN_AGE_MS,
                        exchange_trades_provider=self._fetch_exit_trades,
                    )
                    if closed:
                        logger.warning(
                            "{} reconciled {} local open trade(s) as exchange flat",
                            symbol, closed,
                        )
                    if active_orders:
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

            await self._sync_condition_history(symbol, active_orders)
            if not await self._should_enforce_position_protection(symbol, reason, position):
                continue
            # B4: 刚做完孤儿接管的 symbol，需要重新拉一次 active_orders，
            # 否则后续 has_stop 判断仍会用旧列表，导致已挂的 SL 被误判缺失。
            if self._just_adopted.pop(symbol, False):
                try:
                    active_orders = await self._active_protection_orders(symbol)
                except Exception as e:
                    logger.warning(
                        "reconcile {} post-adopt active protection query failed: {}",
                        symbol, e,
                    )

            side = position["side"]
            qty = float(position["contracts"] or 0.0)
            entry = float(position["entry_price"] or 0.0)
            mark = await self._current_mark_price(symbol, position)
            close_side = "sell" if side == "long" else "buy"
            await self._report_protection_collection_alerts(
                symbol, active_orders, qty=qty,
            )
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
                try:
                    repair_msg = await self._repair_sl_tp(symbol)
                    logger.warning(
                        "{} SL missing during {}; protection repair result: {}",
                        symbol, reason, repair_msg,
                    )
                    continue
                except Exception as e:
                    logger.error(
                        "{} SL missing during {}; protection repair failed: {}",
                        symbol, reason, e,
                    )
                    if (
                        isinstance(e, ProtectionRepairError)
                        and e.reason_code == "SL_TRIGGER_CROSSED_MARK"
                    ):
                        close_reason = (
                            f"SL protection missing during exchange reconcile ({reason}); "
                            f"{e}"
                        )
                        logger.error(
                            "{} SL repair trigger crossed mark during {}; "
                            "emergency close required: {}",
                            symbol, reason, e,
                        )
                        await self._disable_symbol_and_emergency_close(
                            symbol,
                            reason_code=e.reason_code,
                            reason=close_reason,
                            source=f"engine:{reason}",
                        )
                        continue
                await self._disable_symbol_due_protection_failure(
                    symbol,
                    f"SL protection missing during exchange reconcile ({reason})",
                )
                logger.error(
                    "{} SL missing during {}; symbol disabled, auto close skipped",
                    symbol,
                    reason,
                )

    async def _should_enforce_position_protection(
        self,
        symbol: str,
        reason: str,
        position: dict | None = None,
    ) -> bool:
        """Only auto-fix/close positions that are both enabled and locally managed.

        B4 修复：将孤儿持仓的"禁用"路径拆成"先尝试接管，失败再禁用"。
        - 旧行为：看到交易所持仓 + 本地无 trade + 无 active claim → 直接禁用币种，
          导致 MAKER race 留下的部分成交仓位被丢在交易所 6 小时无 SL/TP。
        - 新行为：先查最近 N 分钟是否有收尾的 canceled/error/filled claim，
          且 claim 的 planned_qty 与当前持仓同量级 → 调 ``_adopt_orphan_position``
          建接管 trade + 触发 SL/TP 补单。完全无 claim 关联才走禁用。
        """
        if not self._symbol_enabled.get(symbol, False):
            logger.warning(
                "{} live position detected during {}, but symbol is disabled; "
                "continue protection ownership check while keeping new entries disabled",
                symbol, reason,
            )
        if self._has_recent_explicit_close(symbol):
            logger.warning(
                "{} live position detected during {}, but explicit close completed recently; "
                "defer unmanaged-position handling",
                symbol, reason,
            )
            return False
        try:
            claimed = await self._store.has_active_position_claim(symbol)
        except Exception as e:
            logger.warning(
                "{} live position detected during {}, but position claim check failed: {}; "
                "skip auto protection enforcement",
                symbol, reason, e,
            )
            return False
        if claimed:
            logger.warning(
                "{} live position detected during {}, but local entry claim is still active; "
                "wait for open flow to finish",
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

        # B4：尝试孤儿接管。返回 True 表示已接管成功（_enforce_exchange_invariants
        # 会继续做 SL/TP 修补）；返回 False 才走禁用。
        adopted = await self._adopt_orphan_position(symbol, reason=reason)
        if adopted:
            return True

        confirmed, live_position = await self._confirm_unmanaged_live_position(
            symbol,
            position,
            reason,
        )
        if not confirmed:
            return False
        if live_position is not None:
            self.runtime.positions[symbol] = live_position
        message = (
            f"{symbol} live position detected during {reason}, but no local open trade "
            "exists; symbol disabled and auto close skipped"
        )
        await self._set_symbol_disabled(
            symbol,
            reason_code="UNMANAGED_LIVE_POSITION",
            reason=message,
            source=f"engine:{reason}",
            action="disable_new_entries",
        )
        logger.error(message)
        await self._notifier.send(Event.ERROR, message)
        return False

    async def _adopt_orphan_position(self, symbol: str, *, reason: str) -> bool:
        # 进入接管前标记"刚接管"：外层 _enforce_exchange_invariants 会基于这个
        # 标记重新拉一次 active_orders，避免用 adoption 前那批已过期的数据。
        if not hasattr(self, "_just_adopted"):
            self._just_adopted = {}
        self._just_adopted[symbol] = True

        """B4：孤儿持仓接管。

        触发条件：交易所确有 0<qty 的持仓 + 本地无 open trade + 无 active claim
        + 最近 15 分钟内有收尾的 canceled/error/filled claim，且方向匹配。

        行为：
        1) 通过 ``ensure_takeover_trade`` 建一条 source='orphan_adoption' 的 open trade
        2) 触发 _repair_sl_tp 流程补 SL/TP（沿用 latest_open_decision 模板的 stop/take pct）
        3) 自动重新启用该币种

        返回 True 表示已接管（外层继续走 SL/TP 修补）；False 表示不该接管。
        """
        try:
            claim = await self._store.latest_finished_position_claim(symbol, within_ms=900_000)
        except Exception as e:
            logger.warning("{} orphan adopt: claim query failed: {}", symbol, e)
            return False
        if not claim:
            return False
        # 只在 claim 来自策略（source=strategy）且有非零 planned_qty 时接管，
        # 避免误把人工外部开仓的仓位也接管进来。
        if claim.get("source") not in ("strategy", "manual", ""):
            return False
        if claim.get("planned_qty", 0) <= 0:
            return False

        # 拉交易所侧实情做交叉验证
        position = await self._fetch_exchange_position(symbol)
        if position is None:
            return False
        side = position.get("side", "")
        qty = float(position.get("contracts") or 0.0)
        entry = float(position.get("entry_price") or 0.0)
        if side not in ("long", "short") or qty <= 0 or entry <= 0:
            return False
        if claim.get("side") and side != claim.get("side"):
            logger.warning(
                "{} orphan adopt skipped: claim side={} but exchange side={}",
                symbol, claim.get("side"), side,
            )
            return False
        # claim 的 planned_qty 应与当前 qty 同量级（至少 0.1x，否则可能是另一笔仓位）
        planned = float(claim.get("planned_qty") or 0.0)
        if planned <= 0:
            return False
        ratio = qty / planned
        if ratio < 0.05 or ratio > 1.5:
            logger.warning(
                "{} orphan adopt skipped: claim planned={} but exchange qty={} (ratio {:.2f})",
                symbol, planned, qty, ratio,
            )
            return False

        # 1) 接管 trade 行
        leverage = int(position.get("leverage") or 0)
        try:
            trade_id = await self._store.ensure_takeover_trade(
                symbol=symbol,
                direction=side,
                qty=qty,
                entry_price=entry,
                leverage=leverage,
                source="orphan_adoption",
            )
        except Exception as e:
            logger.warning("{} orphan adopt: ensure_takeover_trade failed: {}", symbol, e)
            return False

        # 2) 重新启用 + 准备 SL/TP
        self._symbol_enabled[symbol] = True
        await self._store.set_symbol_enabled(symbol, True)
        mark = await self._current_mark_price(symbol, position)
        position["markPrice"] = mark
        self.runtime.positions[symbol] = position
        message = (
            f"{symbol} orphan position adopted: side={side} qty={qty} entry={entry} "
            f"leverage={leverage} trade_id={trade_id} reason={reason}"
        )
        logger.warning(message)
        await self._notifier.send(Event.ERROR, message)

        # 3) 触发 SL/TP 补单。优先用最近 OPEN 决策的 stop/take pct 算触发价
        # （MAKER race 留下的孤儿最常见），再用 _repair_sl_tp 兜底（适用于人工
        # 外部开仓后被识别的场景）。
        placed_specs: list[tuple[str, str, float]] = []
        try:
            decision = await self._store.latest_open_decision(symbol)
        except Exception as e:
            logger.warning("{} orphan adopt: latest_open_decision failed: {}", symbol, e)
            decision = None
        if decision and decision.get("stop_loss_pct", 0) > 0:
            # 构造一个临时的"决策视图"给 _planned_protection_specs 用
            from src.llm.schema import Action, TradeDecision as _TD
            try:
                raw_plan = str(decision.get("take_profit_plan_json") or "")
                take_profit_targets = json.loads(raw_plan) if raw_plan else []
                tdec = _TD(
                    symbol=symbol,
                    action=Action.OPEN_LONG if side == "long" else Action.OPEN_SHORT,
                    confidence=1.0,
                    size_pct=0.0,
                    leverage=leverage or 1,
                    stop_loss_pct=float(decision.get("stop_loss_pct") or 0.0),
                    take_profit_pct=(
                        0.0 if take_profit_targets
                        else float(decision.get("take_profit_pct") or 0.0)
                    ),
                    take_profit_targets=take_profit_targets,
                    reason="orphan_adoption",
                )
                placed_specs = self._planned_protection_specs(tdec, entry, qty)
            except Exception as e:
                logger.warning("{} orphan adopt: build specs failed: {}", symbol, e)

        try:
            if placed_specs:
                results = await self._executor.place_protection_orders(
                    symbol=symbol,
                    pos_side=side,
                    qty=qty,
                    specs=placed_specs,
                )
                for order in results:
                    order["trade_id"] = trade_id
                    if leverage > 0:
                        order["leverage"] = leverage
                        order["margin"] = float(order.get("notional") or 0.0) / leverage
                    await self._store.log_order(order)
                placed = [
                    f"{o['kind']}@{float(o.get('price') or 0.0):.2f}"
                    for o in results if o.get("status") == "placed"
                ]
                logger.warning(
                    "{} orphan adopt: placed SL/TP from latest decision: {}",
                    symbol, placed,
                )
            else:
                # 兜底走 _repair_sl_tp
                repair_msg = await self._repair_sl_tp(symbol)
                logger.warning("{} orphan adopt: repair_sl_tp -> {}", symbol, repair_msg)
        except Exception as e:
            logger.warning("{} orphan adopt: SL/TP placement failed: {}", symbol, e)
            # 接管成功但 SL/TP 失败时仍返回 True —— _enforce_exchange_invariants
            # 会再走 _handle_missing_stop_after_open 路径决定是否进一步禁用。
        return True

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
        try:
            await self._rest_resync_with_event_buffer("cycle_snapshot")
        except Exception as e:
            self.runtime.halt_entries("position query failed; retaining last known positions")
            await self._persist_strategy_pause_reason(
                reason_code="POSITION_QUERY_FAILED",
                reason=self.runtime.halt_new_entries_reason,
                source="engine:snapshot",
            )
            logger.error("position snapshot aborted; last known state retained: {}", e)
            return
        new_positions = dict(self.runtime.positions)
        condition_exits = self._detect_external_closes(prev_positions, new_positions)
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

    async def _submit_rest_account_snapshot(self, reason: str) -> None:
        positions = await self._client.fetch_positions(self._tracked_symbols())
        open_orders = await self._fetch_open_orders_safe()
        balance = await self._client.fetch_balance()
        # Unit tests and one-shot maintenance helpers may call snapshot methods without
        # starting the coordinator lifecycle. Keep that path synchronous and explicit.
        if not self._account.started:
            self.runtime.positions = {
                normalize_position(p)["symbol"]: p
                for p in positions
                if normalize_position(p)["contracts"] > 0
            }
            self.runtime.open_orders = {}
            for order in open_orders:
                self.runtime.open_orders.setdefault(
                    normalize_symbol(order.get("symbol")), []
                ).append(order)
            await self._store.snapshot_positions(positions, symbols=self._tracked_symbols())
            if open_orders:
                await self._store.snapshot_open_orders(open_orders)
            await self._sync_exchange_day_pnl()
            await self._record_balance_snapshot(balance)
            return
        await self._account.submit(rest_snapshot_event(
            positions=positions,
            open_orders=open_orders,
            balance=balance,
            reason=reason,
        ))
        await self._account.drain()
        await self._store.snapshot_positions(positions, symbols=self._tracked_symbols())
        if open_orders:
            await self._store.snapshot_open_orders(open_orders)
        await self._sync_exchange_day_pnl()
        await self._record_balance_snapshot(balance)
        await self._store.set_runtime_setting(
            "stream.last_resync_at_ms", str(int(time.time() * 1000))
        )

    async def _record_balance_snapshot(self, balance: dict) -> None:
        # 兜底：ccxt 在限频/限流/接口偶发残缺时，`bal['total'][USDT]`
        # 可能为 None / 缺失 / <= 0。若按"or 0.0"直接落库，会把权益
        # 跌 0 的尖刺写进 balance_snapshots，前端曲线瞬间砸到 0。
        # 此处改为：无效值不写库、不更新 runtime.current_equity，
        # 保留上一周期的权益与可用保证金，下一周期自然恢复。
        quote = self._settings.account.quote_asset
        total_raw = (balance.get("total") or {}).get(quote)
        free_raw = (balance.get("free") or {}).get(quote)
        try:
            total = float(total_raw) if total_raw is not None else 0.0
        except (TypeError, ValueError):
            total = 0.0
        try:
            free = float(free_raw) if free_raw is not None else 0.0
        except (TypeError, ValueError):
            free = 0.0
        if total <= 0 or free < 0:
            logger.warning(
                "balance parse invalid, skip snapshot: total={} free={} (keep prev equity={:.2f})",
                total_raw, free_raw, self.runtime.current_equity,
            )
            return
        self.runtime.update_equity(total)
        await self._store.set_runtime_settings({
            "risk.equity_peak": str(self.runtime.equity_peak),
            _RISK_DAY_KEY: self.runtime.risk_day_key,
            _RISK_DAY_EQUITY_PEAK_KEY: str(self.runtime.risk_day_equity_peak),
            _RISK_DRAWDOWN_BYPASS_DAY_KEY: (
                self.runtime.drawdown_bypass_day
                if self.runtime.drawdown_bypass_active() else ""
            ),
        })
        await self._store.snapshot_balance(
            total_equity=total,
            available_margin=free,
            runtime=self.runtime,
            quote_asset=quote,
        )

    async def _sync_exchange_day_pnl(self) -> None:
        """Recompute today's net realized PnL from Binance's authoritative ledger."""
        if not hasattr(self._client, "fetch_income_history"):
            return
        now = time.time()
        local = time.localtime(now)
        start_ms = int(time.mktime((
            local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0,
            local.tm_wday, local.tm_yday, local.tm_isdst,
        )) * 1000)
        try:
            rows = await self._client.fetch_income_history(start_ms)
        except Exception as e:
            logger.warning("exchange income sync failed; retaining local day pnl: {}", e)
            return
        allowed = {"REALIZED_PNL", "FUNDING_FEE", "COMMISSION"}
        quote = self._settings.account.quote_asset
        total = 0.0
        for row in rows:
            if str(row.get("incomeType") or "") not in allowed:
                continue
            if str(row.get("asset") or quote) != quote:
                continue
            try:
                total += float(row.get("income") or 0.0)
            except (TypeError, ValueError):
                continue
        self.runtime.roll_day_if_needed(now)
        self.runtime.day_realized_pnl = total

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
                    "ts_ms": int(time.time() * 1000),
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
        self.runtime.halt_entries(f"engine stopping/restarting: {reason}")
        self._stopped.set()
        await self._notifier.send(Event.CIRCUIT_BREAK, f"engine stopped: {reason}")

    # ---------- Kill switch ----------
    async def kill(self, reason: str = "manual") -> None:
        """紧急停机：撤单 + 平仓 + 停止循环。"""
        logger.warning("KILL SWITCH triggered: {}", reason)
        self.runtime.trigger_kill()
        self._stopped.set()
        failure: Exception | None = None
        try:
            symbols = self._tracked_symbols()
            await self._executor.cancel_all_orders(symbols=symbols)
            await self._executor.flatten_all(symbols=symbols)
        except Exception as e:
            logger.error("kill switch flatten failed: {}", e)
            failure = e
        await self._notifier.send(Event.KILL_SWITCH, reason)
        if failure is not None:
            raise RuntimeError(f"kill switch incomplete: {failure}") from failure

    # ---------- LLM profile 热替换 ----------
    async def _bootstrap_llm_profile(self) -> None:
        """Build the active DB-backed profile chain; never bootstrap credentials from env."""
        active = await self._store.get_active_llm_profile()
        if active is not None:
            try:
                await self._apply_llm_chain(source="db")
                return
            except Exception as e:  # noqa: BLE001
                logger.error("failed to build stored LLM profile chain: {}", e)
        self.runtime.halt_entries("no active usable LLM profile")
        await self._persist_strategy_pause_reason(
            reason_code="LLM_PROFILE_REQUIRED",
            reason=self.runtime.halt_new_entries_reason,
            source="engine:startup",
        )
        logger.warning(
            "no active usable LLM profile; entries remain paused until a profile is activated"
        )

    async def _build_llm_chain(self):
        """按 priority 升序把 active 主源 + fallback_enabled 备源建成 LLMFailoverClient。"""
        from src.llm.client import LLMClient
        from src.llm.failover import LLMFailoverClient
        profiles = await self._store.get_enabled_llm_profiles()
        prompt = await self._store.get_active_llm_prompt_version()
        prompt_version = int((prompt or {}).get("version") or 0)
        prompt_name = str((prompt or {}).get("name") or "")
        chain: list[tuple[str, LLMClient]] = []
        for prof in profiles:
            api_key = await self._store.get_llm_profile_secret(prof["name"])
            if not api_key:
                logger.warning("llm profile {} has no api_key, skip in chain", prof["name"])
                continue
            client = LLMClient.from_profile(prof, self._settings.llm, api_key)
            client.set_prompt_version(prompt)
            chain.append((prof["name"], client))
        if not chain:
            raise RuntimeError("no usable llm profile (missing api_key)")
        self._llm_prompt_version = prompt_version
        self._llm_prompt_name = prompt_name
        return LLMFailoverClient(chain)

    async def _apply_llm_chain(self, source: str) -> None:
        """重建整条 fallback 链并原子替换。"""
        new_chain = await self._build_llm_chain()
        await self._replace_llm_client(new_chain, new_chain.primary_name, source=source)

    async def _replace_llm_client(
        self, new_client, name: str, source: str
    ) -> None:
        """加锁替换 + 增 version + 落 audit。"""
        async with self._llm_lock:
            old = self._llm
            self._llm = new_client
            self._llm_version += 1
            self._llm_profile_name = name
        try:
            await old.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("old LLMClient close failed: {}", e)
        # 把 version/name 写到 runtime_settings，web 进程能感知"是否真的热替换了"。
        chain_names = getattr(new_client, "source_names", [name])
        try:
            await self._store.set_runtime_settings({
                "llm.active_name": name,
                "llm.active_version": str(self._llm_version),
                "llm.active_source": source,
                "llm.chain": ",".join(chain_names),
                "llm.prompt_version": str(self._llm_prompt_version),
                "llm.prompt_name": self._llm_prompt_name,
                "llm.prompt_source": source,
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("failed to persist LLM active runtime settings: {}", e)
        # 审计：写到 decisions 表里便于复盘
        try:
            await self._store.log_audit(
                symbol="__LLM_SWITCH__",
                action="LLM_SWITCH",
                reason=(
                    f"profile: {name} chain={chain_names} "
                    f"prompt=v{self._llm_prompt_version}:{self._llm_prompt_name} "
                    f"(source={source})"
                ),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("failed to log LLM switch audit: {}", e)
        logger.warning(
            "LLM profile switched: name={} chain={} source={} version={}",
            name, chain_names, source, self._llm_version,
        )

    async def _switch_llm_profile(self, arg: str) -> str:
        name = (arg or "").strip()
        if not name:
            raise ValueError("SWITCH_LLM_PROFILE requires a profile name")
        prof = await self._store.get_llm_profile(name)
        if prof is None:
            raise ValueError(f"llm profile not found: {name!r}")
        # 记住旧 active，建链失败时回滚 DB，避免 DB 与引擎实际状态脱节。
        old_name = self._llm_profile_name
        try:
            await self._apply_llm_chain(source="command")
        except Exception:
            if old_name:
                try:
                    await self._store.activate_llm_profile(old_name)
                    logger.warning(
                        "SWITCH_LLM_PROFILE failed, rolled back DB is_active to {}", old_name
                    )
                except Exception as roll_e:
                    logger.warning("llm profile rollback failed: {}", roll_e)
            raise
        return f"llm profile switched: {name} (version={self._llm_version})"

    async def _reload_llm_prompt(self) -> str:
        await self._apply_llm_chain(source="prompt")
        return (
            f"llm prompt reloaded: v{self._llm_prompt_version} "
            f"{self._llm_prompt_name!r} (llm version={self._llm_version})"
        )
