"""主循环调度：节流 → 特征 → LLM → 风控 → 执行 → 落库 → 告警。

本模块是「调度层」，自身不实现风控/执行细节，只负责按 SPEC 的下单前流水线
把各模块串起来，并处理：
- 全局熔断（日亏/回撤）最高优先级检查
- 5 分钟周期按 wall-clock 对齐（扣除本周期耗时）
- kill-switch：撤单 + 平仓 + 停机

dry_run 与真实下单的差异完全封装在 Executor 内，engine 不感知。
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from src.config.schema import Credentials, Settings
from src.exchange.client import ExchangeClient
from src.exchange.market_data import MarketData
from src.execution.executor import Executor, realized_pnl
from src.features.builder import build_context, build_position_snapshot
from src.llm.client import LLMClient
from src.llm.schema import Action, MarketContext, TradeDecision
from src.notify.telegram import Event, Notifier
from src.risk.manager import RiskContext, validate
from src.state.runtime import RuntimeState
from src.store.repo import Store
from src.throttle.gate import should_call_llm


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
        self.runtime = RuntimeState()
        self._stopped = asyncio.Event()

    # ---------- 生命周期 ----------
    async def startup(self) -> None:
        await self._store.connect()
        await self._client.load_markets()
        await self._market.start()
        self.runtime.roll_day_if_needed()
        if self._settings.storage.reconcile_on_start:
            try:
                positions = await self._client.fetch_positions()
                open_orders = await self._fetch_open_orders_safe()
                await self._store.reconcile(positions, self.runtime, open_orders)
            except Exception as e:
                logger.warning("startup reconcile failed: {}", e)
        # 启动即拉一次权益，确保第一个周期的风控/上限基于真实权益而非退回保证金
        try:
            bal = await self._client.fetch_balance()
            total = (bal.get("total") or {}).get(self._settings.account.quote_asset)
            if total is not None:
                self.runtime.update_equity(float(total))
        except Exception as e:
            logger.warning("startup equity fetch failed: {}", e)
        logger.info("engine started (mode={}, dry_run={}, equity={:.2f})",
                    self._settings.mode.value, self._settings.execution.dry_run,
                    self.runtime.current_equity)

    async def shutdown(self) -> None:
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
        elapsed = time.monotonic() - cycle_start
        remaining = max(0.0, interval - elapsed)
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            pass  # 正常超时即到下一周期

    # ---------- 单周期 ----------
    async def _run_cycle(self) -> None:
        self.runtime.roll_day_if_needed()
        await self._process_commands()
        if self.runtime.kill_switch or self._stopped.is_set():
            return
        await self._market.refresh_all()

        # 0. 全局熔断（最高优先级）：日亏 / 回撤
        if await self._check_circuit_breaker():
            return

        # 逐 symbol 处理
        for symbol in self._settings.symbols:
            try:
                await self._process_symbol(symbol)
            except Exception as e:
                logger.exception("process {} failed: {}", symbol, e)

        # 周期收尾：余额/持仓快照
        await self._snapshot()

    async def _process_commands(self) -> None:
        """消费 web 操作面板下发的控制命令（Q1 方案A：解耦命令队列）。

        web 进程只写 control_commands 表，绝不直接碰交易所；命令在这里由交易进程
        串行执行，避免与主循环状态打架。延迟上限为一个周期。
        """
        try:
            commands = await self._store.fetch_pending_commands()
        except Exception as e:
            logger.warning("fetch commands failed: {}", e)
            return
        for cmd in commands:
            name = cmd["command"]
            try:
                result = await self._exec_command(name, cmd.get("arg", ""))
                await self._store.mark_command(cmd["id"], "done", result)
                logger.info("command {} done: {}", name, result)
            except Exception as e:
                await self._store.mark_command(cmd["id"], "failed", str(e))
                logger.error("command {} failed: {}", name, e)

    async def _exec_command(self, name: str, arg: str) -> str:
        """执行单条命令，返回结果描述。未知命令抛错。"""
        if name == "KILL_SWITCH":
            await self.kill("web kill-switch")
            return "kill switch executed (cancel+flatten+stop)"
        if name == "PAUSE":
            self.runtime.halt_new_entries = True
            await self._notifier.send(Event.CIRCUIT_BREAK, "paused via web (no new entries)")
            return "new entries halted"
        if name == "RESUME":
            self.runtime.halt_new_entries = False
            return "new entries resumed"
        if name == "SET_DRY_RUN":
            val = arg.strip().lower() in ("1", "true", "yes", "on")
            self._settings.execution.dry_run = val
            return f"dry_run set to {val}"
        raise ValueError(f"unknown command: {name}")

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
                await self._executor.flatten_all()
            except Exception as e:
                logger.error("circuit-breaker flatten failed: {}", e)
            await self._notifier.send(Event.CIRCUIT_BREAK, breached)
            return True
        return rt.halt_new_entries

    async def _process_symbol(self, symbol: str) -> None:
        snap = self._market.snapshot(symbol)
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
        total_margin = sum(self._position_margin(s) for s in self._settings.symbols)
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

        # 4. 执行（精度规整在 executor 内）
        result = await self._executor.open_position(
            decision=decision, qty=verdict.qty, price=ctx.last_price
        )
        await self._store.log_order(result)
        if result["status"] == "rejected":
            await self._notifier.send(Event.REJECT, f"{symbol} below min order")
            return
        if result["filled"]:
            self.runtime.mark_order_event(symbol)
            await self._notifier.send(
                Event.OPEN, f"{symbol} {decision.action.value} qty={result['qty']} "
                f"notional={result['notional']:.2f} (dry={result['dry_run']})"
            )
            if self._settings.execution.attach_sl_tp:
                sltp = await self._executor.place_sl_tp(
                    decision=decision, entry_price=result["price"], qty=result["qty"]
                )
                for o in sltp:
                    await self._store.log_order(o)

    async def _handle_close(self, symbol: str) -> None:
        raw = self.runtime.positions.get(symbol)
        if not raw:
            logger.info("[{}] CLOSE requested but no position", symbol)
            return
        result = await self._executor.close_position(raw)
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
            # 已显式平仓：从运行态移除，避免 _snapshot 的差异检测重复计账
            self.runtime.positions.pop(symbol, None)
            self.runtime.mark_order_event(symbol)
            await self._notifier.send(
                Event.CLOSE,
                f"{symbol} closed pnl={pnl:.2f} day_pnl={self.runtime.day_realized_pnl:.2f} "
                f"(dry={result['dry_run']})",
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
            return await self._client.fetch_positions()
        except Exception as e:
            logger.warning("fetch positions failed: {}", e)
            return []

    async def _fetch_open_orders_safe(self) -> list[dict]:
        """启动对账用：拉取所有 symbol 的未完成挂单，失败返回空列表。"""
        out: list[dict] = []
        for sym in self._settings.symbols:
            try:
                out.extend(await self._client.fetch_open_orders(sym))
            except Exception as e:
                logger.warning("fetch open orders failed {}: {}", sym, e)
        return out

    async def _snapshot(self) -> None:
        """刷新持仓/余额快照，更新运行态权益与回撤。

        差异检测：上一周期存在、本周期消失的持仓，视为被 SL/TP 或交易所侧外部平仓，
        用「入场价 vs 最后已知标记价」估算其已实现盈亏并累加（驱动日亏熔断）。
        显式 CLOSE 已在 _handle_close 计账并从 runtime.positions 移除，不会重复。
        """
        prev_positions = dict(self.runtime.positions)
        positions = await self._fetch_positions_safe()
        new_positions = {
            (p.get("symbol") or "").replace("/USDT:USDT", "USDT"): p for p in positions
        }
        self._detect_external_closes(prev_positions, new_positions)
        self.runtime.positions = new_positions
        await self._store.snapshot_positions(positions)
        try:
            bal = await self._client.fetch_balance()
            total = (bal.get("total") or {}).get(self._settings.account.quote_asset) or 0.0
            free = (bal.get("free") or {}).get(self._settings.account.quote_asset) or 0.0
            total = float(total)
            self.runtime.update_equity(total)
            await self._store.snapshot_balance(
                total_equity=total, available_margin=float(free), runtime=self.runtime,
                quote_asset=self._settings.account.quote_asset,
            )
        except Exception as e:
            logger.warning("balance snapshot failed: {}", e)

    def _detect_external_closes(self, prev: dict[str, dict], curr: dict[str, dict]) -> None:
        """对比前后持仓，对消失的持仓估算已实现盈亏并累加。

        估算用最后已知标记价作为出场价（SL/TP 实际触发价与之接近，未计手续费），
        是近似值；精确对账以交易所 income 流水为准（见 RUNBOOK 复盘章节）。
        """
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
            logger.info("[{}] external close detected, est. pnl={:.2f} day_pnl={:.2f}",
                        symbol, pnl, self.runtime.day_realized_pnl)

    # <!-- APPEND_CYCLE -->

    # ---------- Kill switch ----------
    async def kill(self, reason: str = "manual") -> None:
        """紧急停机：撤单 + 平仓 + 停止循环。"""
        logger.warning("KILL SWITCH triggered: {}", reason)
        self.runtime.trigger_kill()
        self._stopped.set()
        try:
            await self._executor.cancel_all_orders()
            await self._executor.flatten_all()
        except Exception as e:
            logger.error("kill switch flatten failed: {}", e)
        await self._notifier.send(Event.KILL_SWITCH, reason)
