"""engine/loop.py 测试：熔断优先级、跳过落库、开仓流水线（全 dry-run + 假 I/O）。

不触网：构造 TradingEngine 后替换其 collaborators 为假对象，直接驱动内部方法。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.config.schema import Credentials
from src.engine.loop import TradingEngine
from src.exchange.filters import SymbolFilters
from src.exchange.market_data import SymbolSnapshot
from src.llm.schema import (
    Action,
    IndicatorSnapshot,
    MarketContext,
    PositionSnapshot,
    TradeDecision,
)
from src.notify.telegram import Event


@pytest.fixture
def creds():
    return Credentials(
        binance_api_key="k", binance_api_secret="s", anthropic_api_key="a",
    )


class FakeStore:
    def __init__(self):
        self.decisions = []
        self.rejects = []
        self.orders = []
        self.condition_exits = []
        self.templates = {}
        self.latest_decision = None
        self.runtime_settings = {}
        self.pending = []          # 待执行命令
        self.marked = []           # (id, status, result)
        self.position_snapshots = []
        self.balance_snapshots = []
        self.open_order_snapshots = []

    async def log_decision(self, **kw):
        self.decisions.append(kw)

    async def log_reject(self, **kw):
        self.rejects.append(kw)

    async def log_order(self, order):
        self.orders.append(order)

    async def snapshot_positions(self, positions, symbols=None):
        self.position_snapshots.append((positions, symbols))

    async def snapshot_balance(self, **kw):
        self.balance_snapshots.append(kw)

    async def mark_condition_exit(self, **kw):
        self.condition_exits.append(kw)

    async def latest_protection_templates(self, symbol, *, dry_run=None):
        return self.templates

    async def latest_open_decision(self, symbol):
        return self.latest_decision

    async def set_runtime_setting(self, key, value):
        self.runtime_settings[key] = value

    async def get_runtime_setting(self, key):
        return self.runtime_settings.get(key)

    async def fetch_pending_commands(self):
        out, self.pending = self.pending, []
        return out

    async def mark_command(self, cmd_id, status, result=""):
        self.marked.append((cmd_id, status, result))

    async def mark_orders_status_by_exchange_ids(self, exchange_order_ids, status):
        return 0

    async def mark_symbol_conditions_not_live(self, symbol, live_exchange_order_ids, status="canceled"):
        return 0

    async def snapshot_open_orders(self, orders):
        self.open_order_snapshots.append(orders)


class FakeClient:
    def __init__(self):
        self.open_orders = []
        self.condition_orders = []
        self.positions = []
        self.canceled_condition_symbols = []
        self.canceled_condition_orders = []

    async def fetch_open_orders(self, symbol=None):
        return self.open_orders

    async def fetch_open_condition_orders(self, symbol):
        return self.condition_orders

    async def fetch_positions(self, symbols=None):
        return self.positions

    async def fetch_balance(self):
        return {"total": {"USDT": 200.0}, "free": {"USDT": 200.0}}

    async def fetch_ticker(self, symbol):
        return {"mark": 100.0, "last": 100.0}

    async def cancel_condition_order(self, symbol, order_id, *, client_algo_id=""):
        self.canceled_condition_orders.append((symbol, order_id, client_algo_id))

    async def cancel_all_condition_orders(self, symbol=None):
        self.canceled_condition_symbols.append(symbol)
        self.condition_orders = []
        return []


class FakeNotifier:
    def __init__(self):
        self.events = []

    async def send(self, event, message):
        self.events.append((event, message))
        return True


class FakeExecutor:
    def __init__(self):
        self.flattened = 0
        self.canceled = 0
        self.opened = []

    async def flatten_all(self):
        self.flattened += 1
        return []

    async def cancel_all_orders(self):
        self.canceled += 1

    async def open_position(self, *, decision, qty, price):
        self.opened.append((decision.symbol, qty))
        return {"symbol": decision.symbol, "kind": "OPEN", "status": "dry_run",
                "filled": True, "opened": True, "qty": qty, "price": price,
                "notional": qty * price, "dry_run": True, "side": "buy", "id": ""}

    async def place_sl_tp(self, *, decision, entry_price, qty):
        return []

    async def place_protection_orders(self, *, symbol, pos_side, qty, specs):
        return [
            {"symbol": symbol, "kind": kind, "side": "sell" if pos_side == "long" else "buy",
             "order_type": otype, "qty": qty, "price": trigger, "notional": qty * trigger,
             "dry_run": False, "status": "placed", "id": f"{kind}-1", "raw": {}}
            for kind, otype, trigger in specs
        ]

    async def close_position(self, position):
        return {"symbol": "BTCUSDT", "kind": "CLOSE", "status": "dry_run",
                "filled": True, "closed": True, "dry_run": True,
                "qty": abs(float(position.get("contracts") or 0)),
                "price": float(position.get("markPrice") or 0),
                "entry_price": float(position.get("entryPrice") or 0),
                "pos_side": (position.get("side") or "").lower()}


def _engine(settings, creds, monkeypatch):
    eng = TradingEngine(settings, creds)
    eng._store = FakeStore()
    eng._notifier = FakeNotifier()
    eng._executor = FakeExecutor()
    eng._client = FakeClient()
    return eng


def _snap(price=100.0):
    s = SymbolSnapshot(symbol="BTCUSDT")
    s.last_price = price
    s.mark_price = price
    s.updated_ms = 1
    s.klines = [[i * 60000, price, price + 1, price - 1, price, 10.0] for i in range(60)]
    return s


def _ctx(price=100.0, margin=200.0):
    return MarketContext(
        symbol="BTCUSDT",
        timestamp=1,
        last_price=price,
        mark_price=price,
        funding_rate=0.0,
        change_24h_pct=0.0,
        recent_klines=[[1, price, price, price, price, 1.0]] * 25,
        indicators=IndicatorSnapshot(
            ema_fast=price,
            ema_slow=price,
            rsi=50,
            macd=0,
            macd_signal=0,
            atr=1,
            boll_upper=price + 1,
            boll_lower=price - 1,
        ),
        position=PositionSnapshot(),
        available_margin=margin,
        max_leverage_allowed=3,
    )


async def test_circuit_breaker_trips_on_daily_loss(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.update_equity(200.0)  # 权益基准
    # 日亏限额 = 200 * 10% = 20；亏 21 触发
    limit = 200.0 * settings.risk.daily_max_loss_pct / 100.0
    eng.runtime.day_realized_pnl = -limit - 1
    tripped = await eng._check_circuit_breaker()
    assert tripped is True
    assert eng.runtime.halt_new_entries is True
    assert eng._executor.flattened == 1
    assert any(e == Event.CIRCUIT_BREAK for e, _ in eng._notifier.events)


async def test_circuit_breaker_trips_on_drawdown(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.drawdown_pct = settings.risk.max_drawdown_pct + 1
    assert await eng._check_circuit_breaker() is True
    assert eng._executor.flattened == 1


async def test_no_breaker_under_limits(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.day_realized_pnl = -1.0
    eng.runtime.drawdown_pct = 1.0
    assert await eng._check_circuit_breaker() is False
    assert eng._executor.flattened == 0


async def test_paused_cycle_still_snapshots(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.halt_new_entries = True

    async def refresh_all():
        return None
    monkeypatch.setattr(eng._market, "refresh_all", refresh_all)

    await eng._run_cycle()

    assert eng._store.position_snapshots
    assert eng._store.balance_snapshots


async def test_record_balance_snapshot_updates_runtime(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)

    await eng._record_balance_snapshot({"total": {"USDT": 321.0}, "free": {"USDT": 300.0}})

    assert eng.runtime.current_equity == pytest.approx(321.0)
    assert eng._store.balance_snapshots[0]["total_equity"] == pytest.approx(321.0)
    assert eng._store.balance_snapshots[0]["available_margin"] == pytest.approx(300.0)


async def test_sync_open_orders_includes_condition_orders(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._client.open_orders = [
        {"id": "limit-1", "symbol": "BTC/USDT:USDT", "type": "limit", "status": "open"},
    ]
    eng._client.condition_orders = [
        {"id": "tp-1", "symbol": "BTC/USDT:USDT", "type": "TAKE_PROFIT_MARKET",
         "side": "sell", "amount": 0.1, "stopPrice": 105.0, "status": "open",
         "reduceOnly": True},
    ]

    await eng._sync_open_orders_snapshot()

    assert len(eng.runtime.open_orders["BTCUSDT"]) == 2
    assert len(eng._store.open_order_snapshots[0]) == 2


async def test_reconcile_disables_symbol_when_stale_condition_remains(settings, creds, monkeypatch):
    settings.execution.dry_run = False
    eng = _engine(settings, creds, monkeypatch)
    eng._client.condition_orders = [
        {"id": "tp-old", "symbol": "BTC/USDT:USDT", "type": "TAKE_PROFIT_MARKET",
         "side": "buy", "amount": 0.5, "stopPrice": 95.0, "status": "open",
         "reduceOnly": True},
    ]

    await eng._enforce_exchange_invariants("test")

    assert eng._client.canceled_condition_orders
    assert eng._symbol_enabled["BTCUSDT"] is False
    assert eng._store.runtime_settings["symbol.enabled.BTCUSDT"] == "false"


async def test_skip_logs_decision(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    # 给一个已有决策价，且价格不动 → 跳过
    eng.runtime.record_decision("BTCUSDT", 100.0)
    monkeypatch.setattr(eng._market, "snapshot", lambda s: _snap(100.0))
    await eng._process_symbol("BTCUSDT")
    assert len(eng._store.decisions) == 1
    assert eng._store.decisions[0]["skipped"] is True
    assert eng._executor.opened == []


async def test_open_pipeline_passes_risk_and_executes(settings, creds, monkeypatch):
    settings.risk.max_leverage = 5
    settings.risk.min_confidence = 0.6
    eng = _engine(settings, creds, monkeypatch)
    monkeypatch.setattr(eng._market, "snapshot", lambda s: _snap(100.0))

    async def fake_margin():
        return 200.0
    monkeypatch.setattr(eng, "_fetch_margin_safe", fake_margin)

    async def fake_decide(ctx):
        return TradeDecision(symbol="BTCUSDT", action=Action.OPEN_LONG, confidence=0.9,
                             size_pct=0.05, leverage=2, stop_loss_pct=0.02,
                             take_profit_pct=0.04, reason="ok")
    monkeypatch.setattr(eng._llm, "decide", fake_decide)

    await eng._process_symbol("BTCUSDT")
    assert eng._executor.opened and eng._executor.opened[0][0] == "BTCUSDT"
    assert any(e == Event.OPEN for e, _ in eng._notifier.events)
    assert eng._store.rejects == []


async def test_open_pipeline_rejects_high_leverage(settings, creds, monkeypatch):
    settings.risk.max_leverage = 3
    eng = _engine(settings, creds, monkeypatch)
    monkeypatch.setattr(eng._market, "snapshot", lambda s: _snap(100.0))

    async def fake_margin():
        return 200.0
    monkeypatch.setattr(eng, "_fetch_margin_safe", fake_margin)

    async def fake_decide(ctx):
        return TradeDecision(symbol="BTCUSDT", action=Action.OPEN_LONG, confidence=0.9,
                             size_pct=0.05, leverage=10, stop_loss_pct=0.02,
                             take_profit_pct=0.04, reason="too much")
    monkeypatch.setattr(eng._llm, "decide", fake_decide)

    await eng._process_symbol("BTCUSDT")
    assert eng._executor.opened == []
    assert len(eng._store.rejects) == 1
    assert any(e == Event.REJECT for e, _ in eng._notifier.events)


async def test_open_rejects_stale_condition_without_position(settings, creds, monkeypatch):
    settings.risk.max_leverage = 5
    settings.risk.min_confidence = 0.6
    settings.execution.dry_run = False
    eng = _engine(settings, creds, monkeypatch)
    eng._client.condition_orders = [
        {"id": "tp-old", "symbol": "BTC/USDT:USDT", "type": "TAKE_PROFIT_MARKET",
         "side": "buy", "amount": 0.5, "stopPrice": 95.0, "status": "open",
         "reduceOnly": True},
    ]
    monkeypatch.setattr(eng._market, "snapshot", lambda s: _snap(100.0))

    async def fake_margin():
        return 200.0
    monkeypatch.setattr(eng, "_fetch_margin_safe", fake_margin)

    async def fake_decide(ctx):
        return TradeDecision(symbol="BTCUSDT", action=Action.OPEN_SHORT, confidence=0.9,
                             size_pct=0.05, leverage=2, stop_loss_pct=0.02,
                             take_profit_pct=0.04, reason="ok")
    monkeypatch.setattr(eng._llm, "decide", fake_decide)

    await eng._process_symbol("BTCUSDT")

    assert eng._executor.opened == []
    assert eng._symbol_enabled["BTCUSDT"] is False
    assert eng._store.rejects[0]["verdict"].code.value == "STALE_CONDITION_ORDER"


class MissingStopExecutor(FakeExecutor):
    def __init__(self):
        super().__init__()
        self.closed = 0

    async def open_position(self, *, decision, qty, price):
        self.opened.append((decision.symbol, qty))
        return {"symbol": decision.symbol, "kind": "OPEN", "status": "filled",
                "filled": True, "opened": True, "qty": qty, "price": price,
                "notional": qty * price, "dry_run": False, "side": "sell", "id": "open-1"}

    async def place_sl_tp(self, *, decision, entry_price, qty):
        return [
            {"symbol": decision.symbol, "kind": "SL", "side": "buy",
             "order_type": "STOP_MARKET", "qty": qty, "price": entry_price * 1.02,
             "notional": 0.0, "dry_run": False, "status": "error", "id": "",
             "raw": {"error": "timeout"}},
        ]

    async def close_position(self, position):
        self.closed += 1
        return {"symbol": "BTCUSDT", "kind": "CLOSE", "status": "filled",
                "filled": True, "closed": True, "dry_run": False,
                "qty": abs(float(position.get("contracts") or 0)),
                "price": float(position.get("markPrice") or 0),
                "entry_price": float(position.get("entryPrice") or 0),
                "pos_side": (position.get("side") or "").lower(),
                "id": "close-1", "side": "buy"}


async def test_open_closes_position_when_stop_not_confirmed(settings, creds, monkeypatch):
    settings.execution.dry_run = False
    settings.risk.max_leverage = 5
    settings.risk.min_confidence = 0.6
    eng = _engine(settings, creds, monkeypatch)
    eng._executor = MissingStopExecutor()
    eng._client.positions = [{
        "symbol": "BTC/USDT:USDT",
        "side": "short",
        "contracts": 0.2,
        "entryPrice": 100.0,
        "markPrice": 101.0,
    }]
    decision = TradeDecision(symbol="BTCUSDT", action=Action.OPEN_SHORT, confidence=0.9,
                             size_pct=0.05, leverage=2, stop_loss_pct=0.02,
                             take_profit_pct=0.04, reason="ok")

    await eng._handle_open(decision, _ctx())

    assert eng._executor.closed == 1
    assert eng._symbol_enabled["BTCUSDT"] is False
    assert eng._store.runtime_settings["symbol.enabled.BTCUSDT"] == "false"
    assert [o["kind"] for o in eng._store.orders] == ["OPEN", "SL", "CLOSE"]


# ---------- P0: 已实现盈亏接通日亏熔断 ----------
async def test_close_accumulates_realized_pnl(settings, creds, monkeypatch):
    """显式 CLOSE 平仓 → 计算盈亏并累加进 day_realized_pnl。"""
    eng = _engine(settings, creds, monkeypatch)
    # 多头 100 进、110 出、量 2 → pnl = +20
    eng.runtime.positions["BTCUSDT"] = {
        "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 2.0,
        "entryPrice": 100.0, "markPrice": 110.0,
    }
    await eng._handle_close("BTCUSDT")
    assert eng.runtime.day_realized_pnl == pytest.approx(20.0)
    assert "BTCUSDT" not in eng.runtime.positions
    assert any(e == Event.CLOSE for e, _ in eng._notifier.events)


async def test_losing_close_drives_daily_loss(settings, creds, monkeypatch):
    """亏损平仓累计到日亏阈值后，熔断检查应触发。"""
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.update_equity(200.0)  # 日亏限额=200*10%=20
    # 多头 100 进、90 出、量 3 → pnl = -30，超过日亏限额 20
    eng.runtime.positions["BTCUSDT"] = {
        "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 3.0,
        "entryPrice": 100.0, "markPrice": 90.0,
    }
    await eng._handle_close("BTCUSDT")
    assert eng.runtime.day_realized_pnl == pytest.approx(-30.0)
    # 熔断检查现在应判定日亏超限
    assert await eng._check_circuit_breaker() is True
    assert eng.runtime.halt_new_entries is True


async def test_external_close_detected_in_snapshot(settings, creds, monkeypatch):
    """SL/TP 在交易所侧触发 → 持仓消失 → _snapshot 差异检测补记盈亏。"""
    eng = _engine(settings, creds, monkeypatch)
    prev = {"BTCUSDT": {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1.0,
                        "entryPrice": 100.0, "markPrice": 95.0}}
    # 本周期交易所已无持仓
    exits = eng._detect_external_closes(prev, {})
    assert eng.runtime.day_realized_pnl == pytest.approx(-5.0)
    assert eng.runtime.pop_order_event("BTCUSDT") is True
    assert exits == [{
        "symbol": "BTCUSDT",
        "triggered_kind": "SL",
        "qty": 1.0,
        "price": 95.0,
    }]


async def test_external_close_ignores_still_open(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    prev = {"BTCUSDT": {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1.0,
                        "entryPrice": 100.0, "markPrice": 95.0}}
    curr = {"BTCUSDT": {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1.0,
                        "entryPrice": 100.0, "markPrice": 95.0}}
    eng._detect_external_closes(prev, curr)
    assert eng.runtime.day_realized_pnl == 0.0


# ---------- 控制命令执行 ----------
async def test_command_pause_resume(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._store.pending = [{"id": 1, "command": "PAUSE", "arg": ""}]
    await eng._process_commands()
    assert eng.runtime.halt_new_entries is True
    assert eng._store.runtime_settings["strategy.paused"] == "true"
    assert eng._store.marked == [(1, "done", "strategy paused (persisted)")]

    eng._store.pending = [{"id": 2, "command": "RESUME", "arg": ""}]
    await eng._process_commands()
    assert eng.runtime.halt_new_entries is False
    assert eng._store.runtime_settings["strategy.paused"] == "false"
    assert eng._store.marked[-1] == (2, "done", "strategy resumed (persisted)")


async def test_command_set_dry_run(settings, creds, monkeypatch):
    settings.execution.dry_run = True
    eng = _engine(settings, creds, monkeypatch)
    eng._store.pending = [{"id": 1, "command": "SET_DRY_RUN", "arg": "false"}]
    await eng._process_commands()
    assert eng._settings.execution.dry_run is False
    assert eng._store.runtime_settings["execution.dry_run"] == "false"


async def test_runtime_dry_run_applied_on_startup(settings, creds, monkeypatch):
    settings.execution.dry_run = False
    eng = _engine(settings, creds, monkeypatch)
    eng._store.runtime_settings["execution.dry_run"] = "true"
    await eng._apply_runtime_settings()
    assert eng._settings.execution.dry_run is True


async def test_runtime_symbol_enabled_applied_on_startup(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._store.runtime_settings["symbol.enabled.BTCUSDT"] = "false"
    await eng._apply_runtime_settings()
    assert eng._symbol_enabled["BTCUSDT"] is False


async def test_runtime_strategy_paused_applied_on_startup(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._store.runtime_settings["strategy.paused"] = "true"
    await eng._apply_runtime_settings()
    assert eng.runtime.halt_new_entries is True


async def test_command_set_symbol_enabled(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._store.pending = [{"id": 1, "command": "SET_SYMBOL_ENABLED", "arg": "BTCUSDT=false"}]
    await eng._process_commands()
    assert eng._symbol_enabled["BTCUSDT"] is False
    assert eng._store.runtime_settings["symbol.enabled.BTCUSDT"] == "false"


async def test_disabled_symbol_skips_llm(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._symbol_enabled["BTCUSDT"] = False
    monkeypatch.setattr(eng._market, "snapshot", lambda s: _snap(100.0))

    await eng._process_symbol("BTCUSDT")

    assert eng._store.decisions[0]["skipped"] is True
    assert eng._store.decisions[0]["skip_reason"] == "symbol disabled"
    assert eng._executor.opened == []


async def test_command_cancel_and_flatten_keeps_engine_running(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._store.pending = [{"id": 1, "command": "CANCEL_AND_FLATTEN", "arg": ""}]
    await eng._process_commands()
    assert eng._executor.canceled == 1
    assert eng._executor.flattened == 1
    assert eng.runtime.halt_new_entries is True
    assert eng._store.runtime_settings["strategy.paused"] == "true"
    assert eng.runtime.kill_switch is False
    assert eng._stopped.is_set() is False
    assert "flattened 0 positions" in eng._store.marked[0][2]


async def test_command_stop_engine_does_not_flatten(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._store.pending = [{"id": 1, "command": "STOP_ENGINE", "arg": ""}]
    await eng._process_commands()
    assert eng._executor.canceled == 0
    assert eng._executor.flattened == 0
    assert eng.runtime.kill_switch is False
    assert eng._stopped.is_set() is True
    assert eng._store.marked[0][1] == "done"


async def test_command_kill_switch(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._store.pending = [{"id": 1, "command": "KILL_SWITCH", "arg": ""}]
    await eng._process_commands()
    assert eng.runtime.kill_switch is True
    assert eng._executor.flattened == 1


class RepairClient:
    def __init__(self, *, position, open_orders=None, equity=1000.0):
        self.position = position
        self.open_orders = open_orders or []
        self.equity = equity
        self.canceled = []
        self._filters = SymbolFilters(
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )

    def filters(self, symbol):
        return self._filters

    async def fetch_positions(self, symbols=None):
        return [self.position] if self.position else []

    async def fetch_open_condition_orders(self, symbol):
        return self.open_orders

    async def fetch_balance(self):
        return {"total": {"USDT": self.equity}, "free": {"USDT": self.equity}}

    async def fetch_ticker(self, symbol):
        return {"mark": self.position.get("markPrice"), "last": self.position.get("markPrice")}

    async def cancel_condition_order(self, symbol, order_id, *, client_algo_id=""):
        self.canceled.append((symbol, order_id, client_algo_id))

    async def cancel_all_condition_orders(self, symbol=None):
        self.canceled.append((symbol, "ALL", ""))
        self.open_orders = []


async def test_command_repair_sl_tp_places_missing_orders(settings, creds, monkeypatch):
    settings.execution.dry_run = False
    settings.risk.max_loss_per_trade_pct = 10
    eng = _engine(settings, creds, monkeypatch)
    eng._client = RepairClient(
        position={
            "symbol": "BTC/USDT:USDT",
            "side": "short",
            "contracts": 1.0,
            "entryPrice": 100.0,
            "markPrice": 99.0,
        },
        equity=1000.0,
    )
    eng._store.templates = {
        "SL": {"price": 102.0, "order_type": "STOP_MARKET"},
        "TP": {"price": 95.0, "order_type": "TAKE_PROFIT_MARKET"},
    }

    result = await eng._exec_command("REPAIR_SL_TP", "BTCUSDT")

    assert "已补挂 SL@102.00, TP@95.00" in result
    assert [o["kind"] for o in eng._store.orders] == ["SL", "TP"]
    assert all(o["side"] == "buy" for o in eng._store.orders)


async def test_command_repair_sl_tp_rejects_out_of_range_stop(settings, creds, monkeypatch):
    settings.execution.dry_run = False
    settings.risk.max_loss_per_trade_pct = 10
    eng = _engine(settings, creds, monkeypatch)
    eng._client = RepairClient(
        position={
            "symbol": "BTC/USDT:USDT",
            "side": "short",
            "contracts": 1.0,
            "entryPrice": 100.0,
            "markPrice": 103.0,
        },
        open_orders=[
            {"id": "tp-1", "symbol": "BTC/USDT:USDT", "type": "TAKE_PROFIT_MARKET",
             "side": "buy", "amount": 1.0, "stopPrice": 95.0, "status": "open",
             "reduceOnly": True},
        ],
        equity=1000.0,
    )
    eng._store.templates = {
        "SL": {"price": 102.0, "order_type": "STOP_MARKET"},
        "TP": {"price": 95.0, "order_type": "TAKE_PROFIT_MARKET"},
    }
    eng._store.pending = [{"id": 9, "command": "REPAIR_SL_TP", "arg": "BTCUSDT"}]

    await eng._process_commands()

    assert eng._store.orders == []
    assert eng._store.marked[0][1] == "failed"
    assert "空单止损必须高于当前标记价" in eng._store.marked[0][2]


async def test_command_repair_sl_tp_blocks_mismatched_stale_order(settings, creds, monkeypatch):
    settings.execution.dry_run = False
    settings.risk.max_loss_per_trade_pct = 10
    eng = _engine(settings, creds, monkeypatch)
    eng._client = RepairClient(
        position={
            "symbol": "BTC/USDT:USDT",
            "side": "short",
            "contracts": 1.0,
            "entryPrice": 100.0,
            "markPrice": 99.0,
        },
        open_orders=[
            {"id": "tp-old", "symbol": "BTC/USDT:USDT", "type": "TAKE_PROFIT_MARKET",
             "side": "buy", "amount": 0.5, "stopPrice": 95.0, "status": "open",
             "reduceOnly": True},
        ],
        equity=1000.0,
    )
    eng._store.templates = {
        "SL": {"price": 102.0, "order_type": "STOP_MARKET"},
        "TP": {"price": 95.0, "order_type": "TAKE_PROFIT_MARKET"},
    }
    eng._store.pending = [{"id": 10, "command": "REPAIR_SL_TP", "arg": "BTCUSDT"}]

    await eng._process_commands()

    assert eng._store.orders == []
    assert eng._symbol_enabled["BTCUSDT"] is False
    assert eng._store.marked[0][1] == "failed"
    assert "陈旧条件单" in eng._store.marked[0][2]


async def test_command_unknown_marked_failed(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._store.pending = [{"id": 9, "command": "BOGUS", "arg": ""}]
    await eng._process_commands()
    assert eng._store.marked[0][0] == 9
    assert eng._store.marked[0][1] == "failed"
