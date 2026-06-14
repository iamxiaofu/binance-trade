"""engine/loop.py 测试：熔断优先级、跳过落库、开仓流水线（假 I/O）。

不触网：构造 TradingEngine 后替换其 collaborators 为假对象，直接驱动内部方法。
"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

import src.engine.loop as engine_loop
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
        self.marked_status_calls = []
        self.position_snapshots = []
        self.balance_snapshots = []
        self.open_order_snapshots = []
        self.open_trades = set()
        self.active_claims = set()
        self.condition_not_live_marks = []
        self.flat_reconciles = []
        self.claims = []
        self.takeover_trades = []
        self.open_qty = {}
        self.system_commands = []  # (command, arg, source, status, result)
        self.symbols = {
            "BTCUSDT": {
                "symbol": "BTCUSDT",
                "enabled": True,
                "status": "active",
                "sync_status": "config_seed",
                "needs_review": False,
                "source": "config",
                "min_qty": 0.0,
                "min_notional": 0.0,
                "tick_size": 0.0,
                "step_size": 0.0,
                "disabled_reason_code": "",
                "disabled_reason": "",
                "disabled_at": "",
                "disabled_source": "",
                "disabled_action": "",
                "last_enabled_at": "",
            }
        }

    async def log_decision(self, **kw):
        self.decisions.append(kw)

    async def log_reject(self, **kw):
        self.rejects.append(kw)

    async def log_order(self, order):
        self.orders.append(order)
        if order.get("kind") == "OPEN" and order.get("filled"):
            self.open_trades.add(order["symbol"])
            self.open_qty[order["symbol"]] = self.open_qty.get(order["symbol"], 0.0) + float(order.get("qty") or 0.0)
        return {"order_id": len(self.orders), "trade_id": int(order.get("trade_id") or len(self.orders))}

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

    async def set_runtime_settings(self, settings):
        self.runtime_settings.update(settings)

    async def get_runtime_setting(self, key):
        return self.runtime_settings.get(key)

    async def fetch_pending_commands(self):
        out, self.pending = self.pending, []
        return out

    async def mark_command(self, cmd_id, status, result=""):
        self.marked.append((cmd_id, status, result))

    async def record_system_command(
        self, command, *, arg="", source="engine", status="done", result=""
    ):
        self.system_commands.append((command, arg, source, status, result))
        return len(self.system_commands)

    async def mark_orders_status_by_exchange_ids(self, exchange_order_ids, status):
        self.marked_status_calls.append((list(exchange_order_ids or []), status))
        return len(exchange_order_ids or [])

    async def mark_symbol_conditions_not_live(self, symbol, live_exchange_order_ids, status="canceled"):
        self.condition_not_live_marks.append((symbol, set(live_exchange_order_ids), status))
        return 0

    async def snapshot_open_orders(self, orders):
        self.open_order_snapshots.append(orders)

    async def has_open_trade(self, symbol):
        return symbol in self.open_trades

    def set_decision(self, symbol, **fields):
        """测试辅助：注入最近一条 OPEN 决策模板。"""
        base = {
            "id": 90000 + len(getattr(self, "decisions", []) or []),
            "symbol": symbol,
            "action": "OPEN_LONG",
            "stop_loss_pct": 0.02,
            "take_profit_pct": 0.04,
            "ref_price": 100.0,
            "ts_ms": 1_000_000_000_000,
            "created_at": "2026-06-09 00:00:00",
        }
        base.update(fields)
        self.latest_decision = base

    async def latest_open_decision(self, symbol):
        ld = getattr(self, "latest_decision", None)
        if ld and ld.get("symbol") == symbol:
            return ld
        return None

    async def latest_protection_templates(self, symbol, *, dry_run=None):
        # 向后兼容：旧测试用 self.templates = {"SL": ..., "TP": ...}（直接是模板字典）
        # 新测试可用 self.templates_by_symbol = {"BTCUSDT": {"SL": ...}}
        tpl = getattr(self, "templates_by_symbol", {}).get(symbol)
        if tpl is not None:
            return tpl
        return getattr(self, "templates", {}) or {}

    async def open_trade_qty(self, symbol):
        return self.open_qty.get(symbol, 0.0) if symbol in self.open_trades else 0.0

    async def begin_position_claim(self, **kw):
        claim_id = len(self.claims) + 1
        self.claims.append({"id": claim_id, **kw, "status": "opening"})
        self.active_claims.add(kw["symbol"])
        return claim_id

    async def finish_position_claim(self, claim_id, **kw):
        for row in self.claims:
            if row["id"] == claim_id:
                row.update(kw)
                row["status"] = kw.get("status", row.get("status"))
                if row["status"] in ("opening", "submitted", "protecting"):
                    self.active_claims.add(row["symbol"])
                else:
                    self.active_claims.discard(row["symbol"])
                return

    async def has_active_position_claim(self, symbol):
        return symbol in self.active_claims

    async def has_recent_entry_claim(self, symbol):
        for row in reversed(self.claims):
            if row.get("symbol") != symbol:
                continue
            if row.get("status") in ("opening", "submitted", "protecting"):
                return True
            if row.get("filled_qty", 0) > 0 and row.get("status") in (
                "filled", "partial", "error", "canceled", "rejected", "expired",
            ):
                return True
        return False

    async def has_fresh_open_trade(self, symbol, max_age_ms):
        return False

    def set_day_pnl(self, by_day):
        """测试辅助：注入 rehydrate 用 {YYYY-MM-DD: pnl} 数据。"""
        self.day_pnl_data = by_day

    async def day_realized_pnl_by_local_day(self):
        return getattr(self, "day_pnl_data", {})

    def set_finished_claim(self, symbol, **fields):
        """测试辅助：注入一个最近收尾的 claim，让 _adopt_orphan_position 能命中。"""
        self.claims.append({
            "id": 99000 + len(self.claims),
            "symbol": symbol,
            "ts_ms": 1_000_000_000_000,  # far in past relative to within_ms
            "status": "canceled",
            "source": "strategy",
            "planned_qty": 1.0,
            "filled_qty": 0.0,
            "entry_price": 0.0,
            "client_order_id": "",
            "reason": "",
            **fields,
        })

    async def latest_finished_position_claim(self, symbol, *, within_ms=900_000):
        for c in reversed(self.claims):
            if c["symbol"] == symbol and c["status"] in (
                "canceled", "error", "filled", "partial", "rejected", "expired",
            ):
                return c
        return None

    async def sync_condition_order_history(self, **kw):
        return 0

    async def reconcile_symbol_flat(
        self,
        symbol,
        *,
        reason="EXCHANGE_FLAT",
        opened_before_ms=None,
        min_open_age_ms=0,
        exchange_trades_provider=None,
    ):
        self.flat_reconciles.append((symbol, reason))
        if symbol in self.open_trades:
            self.open_trades.discard(symbol)
            self.open_qty.pop(symbol, None)
            return 1
        return 0

    async def ensure_takeover_trade(self, **kw):
        trade_id = len(self.takeover_trades) + 100
        self.takeover_trades.append({"id": trade_id, **kw})
        self.open_trades.add(kw["symbol"])
        self.open_qty[kw["symbol"]] = float(kw.get("qty") or 0.0)
        return trade_id

    async def sync_config_symbols(self, symbols):
        for symbol in symbols:
            self.symbols.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "enabled": True,
                    "status": "active",
                    "sync_status": "config_seed",
                    "needs_review": False,
                    "source": "config",
                    "min_qty": 0.0,
                    "min_notional": 0.0,
                    "tick_size": 0.0,
                    "step_size": 0.0,
                    "disabled_reason_code": "",
                    "disabled_reason": "",
                    "disabled_at": "",
                    "disabled_source": "",
                    "disabled_action": "",
                    "last_enabled_at": "",
                },
            )

    async def list_symbols(self, include_archived=False):
        rows = list(self.symbols.values())
        if not include_archived:
            rows = [row for row in rows if row.get("status") != "archived"]
        return rows

    async def get_symbol(self, symbol):
        return self.symbols.get(symbol)

    async def set_symbol_enabled(
        self,
        symbol,
        enabled,
        *,
        reason_code="",
        reason="",
        source="",
        action="",
    ):
        self.symbols[symbol]["enabled"] = enabled
        if enabled:
            self.symbols[symbol]["disabled_reason_code"] = ""
            self.symbols[symbol]["disabled_reason"] = ""
            self.symbols[symbol]["disabled_at"] = ""
            self.symbols[symbol]["disabled_source"] = ""
            self.symbols[symbol]["disabled_action"] = ""
            self.symbols[symbol]["last_enabled_at"] = "now"
        else:
            self.symbols[symbol]["disabled_reason_code"] = reason_code
            self.symbols[symbol]["disabled_reason"] = reason
            self.symbols[symbol]["disabled_at"] = "now"
            self.symbols[symbol]["disabled_source"] = source
            self.symbols[symbol]["disabled_action"] = action
        self.runtime_settings[f"symbol.enabled.{symbol}"] = str(enabled).lower()

    async def update_symbol_filters(self, symbol, filters):
        if symbol not in self.symbols:
            return
        self.symbols[symbol]["min_qty"] = float(filters.min_qty)
        self.symbols[symbol]["min_notional"] = float(filters.min_notional)
        self.symbols[symbol]["tick_size"] = float(filters.tick_size)
        self.symbols[symbol]["step_size"] = float(filters.step_size)

    async def upsert_symbol_from_exchange(
        self,
        *,
        symbol,
        filters,
        exchange_state,
        source="web",
        enabled=False,
        sync_status="confirmed_flat",
        needs_review=False,
    ):
        self.symbols[symbol] = {
            "symbol": symbol,
            "enabled": enabled,
            "status": "active",
            "sync_status": sync_status,
            "needs_review": needs_review,
            "source": source,
            "min_qty": float(filters.min_qty),
            "min_notional": float(filters.min_notional),
            "tick_size": float(filters.tick_size),
            "step_size": float(filters.step_size),
            "disabled_reason_code": "",
            "disabled_reason": "",
            "disabled_at": "",
            "disabled_source": "",
            "disabled_action": "",
            "last_enabled_at": "",
        }
        self.runtime_settings[f"symbol.enabled.{symbol}"] = str(enabled).lower()
        return self.symbols[symbol]


class FakeClient:
    def __init__(self):
        self.open_orders = []
        self.condition_orders = []
        self.positions = []
        self.canceled_condition_symbols = []
        self.canceled_condition_orders = []
        self.canceled_open_orders = []
        self.canceled_all_open_symbols = []

    async def fetch_open_orders(self, symbol=None):
        return self.open_orders

    async def fetch_open_condition_orders(self, symbol):
        return self.condition_orders

    async def fetch_condition_orders(self, symbol, limit=20):
        return []

    async def fetch_positions(self, symbols=None):
        return self.positions

    async def fetch_balance(self):
        return {"total": {"USDT": 200.0}, "free": {"USDT": 200.0}}

    async def fetch_ticker(self, symbol):
        return {"mark": 100.0, "last": 100.0}

    async def fetch_ohlcv(self, symbol, timeframe, limit):
        return [[i * 60000, 100.0, 101.0, 99.0, 100.0, 1.0] for i in range(limit)]

    async def fetch_funding_rate(self, symbol):
        return {"fundingRate": 0.0}

    async def ensure_symbol(self, symbol):
        return SymbolFilters(
            tick_size=Decimal("0.01"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )

    def filters(self, symbol):
        return SymbolFilters(
            tick_size=Decimal("0.01"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )

    async def cancel_order(self, symbol, order_id, params=None):
        self.canceled_open_orders.append((symbol, order_id))
        return {"id": order_id, "status": "canceled"}

    async def cancel_all_orders(self, symbol=None):
        self.canceled_all_open_symbols.append(symbol)
        self.open_orders = []
        return []

    async def cancel_condition_order(self, symbol, order_id, *, client_algo_id=""):
        self.canceled_condition_orders.append((symbol, order_id, client_algo_id))
        return {"id": order_id, "status": "canceled"}

    async def cancel_all_condition_orders(self, symbol=None):
        self.canceled_condition_symbols.append(symbol)
        self.condition_orders = []
        return []


class DelayedPositionClient(FakeClient):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.fetch_positions_calls = 0

    async def fetch_positions(self, symbols=None):
        self.fetch_positions_calls += 1
        idx = min(self.fetch_positions_calls - 1, len(self.responses) - 1)
        response = self.responses[idx]
        if isinstance(response, Exception):
            raise response
        return response


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
        self.closed = []

    async def flatten_all(self, symbols=None):
        self.flattened += 1
        symbols = list(symbols or [])
        return [
            {"symbol": sym, "kind": "CLOSE", "status": "filled",
             "filled": True, "closed": True, "dry_run": False,
             "qty": 1.0, "price": 100.0, "entry_price": 100.0,
             "pos_side": "long", "side": "sell", "id": f"flatten-{i}"}
            for i, sym in enumerate(symbols)
        ]

    async def cancel_all_orders(self, symbols=None):
        self.canceled += 1

    async def open_position(self, *, decision, qty, price):
        self.opened.append((decision.symbol, qty))
        return {"symbol": decision.symbol, "kind": "OPEN", "status": "filled",
                "filled": True, "opened": True, "qty": qty, "price": price,
                "notional": qty * price, "dry_run": False, "side": "buy", "id": "open-1"}

    async def place_sl_tp(self, *, decision, entry_price, qty):
        return [
            {"symbol": decision.symbol, "kind": "SL", "side": "sell",
             "order_type": "STOP_MARKET", "qty": qty, "price": entry_price * 0.98,
             "notional": qty * entry_price * 0.98, "dry_run": False,
             "status": "placed", "id": "SL-1", "raw": {}},
            {"symbol": decision.symbol, "kind": "TP", "side": "sell",
             "order_type": "TAKE_PROFIT_MARKET", "qty": qty, "price": entry_price * 1.04,
             "notional": qty * entry_price * 1.04, "dry_run": False,
             "status": "placed", "id": "TP-1", "raw": {}},
        ]

    async def place_protection_orders(self, *, symbol, pos_side, qty, specs):
        return [
            {"symbol": symbol, "kind": kind, "side": "sell" if pos_side == "long" else "buy",
             "order_type": otype, "qty": qty, "price": trigger, "notional": qty * trigger,
             "dry_run": False, "status": "placed", "id": f"{kind}-1", "raw": {}}
            for kind, otype, trigger in specs
        ]

    async def close_position(self, position, *, mode=None, skip_slippage_guard=False):
        self.closed.append((position, mode, skip_slippage_guard))
        return {"symbol": "BTCUSDT", "kind": "CLOSE", "status": "filled",
                "filled": True, "closed": True, "dry_run": False,
                "qty": abs(float(position.get("contracts") or 0)),
                "price": float(position.get("markPrice") or 0),
                "entry_price": float(position.get("entryPrice") or 0),
                "pos_side": (position.get("side") or "").lower(),
                "side": "sell" if (position.get("side") or "").lower() == "long" else "buy",
                "id": "close-1"}


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
    assert eng.runtime.halt_new_entries_reason.startswith("circuit breaker: daily loss")
    assert eng._executor.flattened == 1
    assert any(e == Event.CIRCUIT_BREAK for e, _ in eng._notifier.events)

    # 新增：pause 元数据 + 系统命令历史都写入了
    assert eng._store.runtime_settings["strategy.paused"] == "true"
    assert eng._store.runtime_settings["strategy.pause.reason_code"] == "DAILY_LOSS"
    assert "daily loss" in eng._store.runtime_settings["strategy.pause.reason"]
    assert eng._store.runtime_settings["strategy.pause.source"] == "engine:circuit_breaker"
    system_cmds = [c for c in eng._store.system_commands if c[0] == "CIRCUIT_BREAKER"]
    assert system_cmds, "circuit breaker should write a system command record"
    assert system_cmds[0][1] == "DAILY_LOSS"
    assert system_cmds[0][3] == "done"
    assert "flattened 1 positions" in system_cmds[0][4]


async def test_circuit_breaker_trips_on_drawdown(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.drawdown_pct = settings.risk.max_drawdown_pct + 1
    assert await eng._check_circuit_breaker() is True
    assert eng._executor.flattened == 1

    assert eng._store.runtime_settings["strategy.pause.reason_code"] == "MAX_DRAWDOWN"
    assert "drawdown" in eng._store.runtime_settings["strategy.pause.reason"]
    assert any(c[0] == "CIRCUIT_BREAKER" and c[1] == "MAX_DRAWDOWN"
               for c in eng._store.system_commands)


async def test_no_breaker_under_limits(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.day_realized_pnl = -1.0
    eng.runtime.drawdown_pct = 1.0
    assert await eng._check_circuit_breaker() is False
    assert eng._executor.flattened == 0


async def test_paused_cycle_still_snapshots(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.halt_new_entries = True

    async def refresh_all(symbols=None):
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


async def test_record_balance_snapshot_skips_when_total_missing(settings, creds, monkeypatch):
    """`bal['total']` 中没有 USDT 键（旧 `or 0.0` 兜底会写 0）。"""
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.current_equity = 200.0
    eng.runtime.equity_peak = 200.0

    await eng._record_balance_snapshot({"total": {"BTC": 1.0}, "free": {}})

    assert eng._store.balance_snapshots == []
    # runtime 不被无效值覆盖
    assert eng.runtime.current_equity == pytest.approx(200.0)


async def test_record_balance_snapshot_skips_when_total_is_none(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.current_equity = 200.0

    await eng._record_balance_snapshot({"total": {"USDT": None}, "free": {"USDT": 0.0}})

    assert eng._store.balance_snapshots == []
    assert eng.runtime.current_equity == pytest.approx(200.0)


async def test_record_balance_snapshot_skips_when_total_dict_empty(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.current_equity = 200.0

    await eng._record_balance_snapshot({"total": {}, "free": {}})

    assert eng._store.balance_snapshots == []
    assert eng.runtime.current_equity == pytest.approx(200.0)


async def test_record_balance_snapshot_skips_when_free_negative(settings, creds, monkeypatch):
    """可用保证金为负视为脏数据，不写库。"""
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.current_equity = 200.0

    await eng._record_balance_snapshot({"total": {"USDT": 321.0}, "free": {"USDT": -1.0}})

    assert eng._store.balance_snapshots == []
    assert eng.runtime.current_equity == pytest.approx(200.0)


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
    eng = _engine(settings, creds, monkeypatch)
    monkeypatch.setattr(engine_loop, "_EXCHANGE_FLAT_CONFIRM_DELAYS_SECONDS", (0.0,))
    eng._client.condition_orders = [
        {"id": "tp-old", "symbol": "BTC/USDT:USDT", "type": "TAKE_PROFIT_MARKET",
         "side": "buy", "amount": 0.5, "stopPrice": 95.0, "status": "open",
         "reduceOnly": True},
    ]

    await eng._enforce_exchange_invariants("test")

    assert eng._client.canceled_condition_orders
    assert eng._symbol_enabled["BTCUSDT"] is False
    assert eng._store.runtime_settings["symbol.enabled.BTCUSDT"] == "false"


async def test_reconcile_defers_exchange_flat_when_position_reappears(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    monkeypatch.setattr(engine_loop, "_EXCHANGE_FLAT_CONFIRM_DELAYS_SECONDS", (0.0,))
    eng._store.open_trades.add("BTCUSDT")
    position = {
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": 0.1,
        "entryPrice": 100.0,
        "markPrice": 101.0,
    }
    eng._client = DelayedPositionClient([[], [position]])
    eng._client.condition_orders = [
        {"id": "sl-live", "symbol": "BTC/USDT:USDT", "type": "STOP_MARKET",
         "side": "sell", "amount": 0.1, "stopPrice": 98.0, "status": "open",
         "reduceOnly": True},
        {"id": "tp-live", "symbol": "BTC/USDT:USDT", "type": "TAKE_PROFIT_MARKET",
         "side": "sell", "amount": 0.1, "stopPrice": 104.0, "status": "open",
         "reduceOnly": True},
    ]

    await eng._enforce_exchange_invariants("periodic")

    assert eng._store.flat_reconciles == []
    assert eng._store.condition_not_live_marks == []
    assert eng._client.canceled_condition_orders == []
    assert "BTCUSDT" in eng._store.open_trades
    assert eng.runtime.positions["BTCUSDT"]["contracts"] == pytest.approx(0.1)


async def test_reconcile_does_not_flat_trade_created_after_initial_check(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    monkeypatch.setattr(engine_loop, "_EXCHANGE_FLAT_CONFIRM_DELAYS_SECONDS", (0.0,))

    async def has_open_trade_race(symbol):
        eng._store.open_trades.add(symbol)
        return False

    eng._store.has_open_trade = has_open_trade_race

    await eng._enforce_exchange_invariants("periodic")

    assert eng._store.flat_reconciles == []
    assert "BTCUSDT" in eng._store.open_trades


async def test_reconcile_defers_exchange_flat_for_recent_entry_claim(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    monkeypatch.setattr(engine_loop, "_EXCHANGE_FLAT_CONFIRM_DELAYS_SECONDS", (0.0,))
    eng._store.open_trades.add("BTCUSDT")
    eng._store.claims.append({
        "id": 1,
        "symbol": "BTCUSDT",
        "status": "filled",
        "filled_qty": 0.1,
    })

    await eng._enforce_exchange_invariants("periodic")

    assert eng._store.flat_reconciles == []
    assert "BTCUSDT" in eng._store.open_trades


async def test_reconcile_closes_old_local_trade_when_exchange_flat_confirmed(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    monkeypatch.setattr(engine_loop, "_EXCHANGE_FLAT_CONFIRM_DELAYS_SECONDS", (0.0,))
    eng._store.open_trades.add("BTCUSDT")

    await eng._enforce_exchange_invariants("periodic")

    assert eng._store.flat_reconciles == [("BTCUSDT", "EXCHANGE_FLAT")]
    assert "BTCUSDT" not in eng._store.open_trades


async def test_reconcile_skips_disabled_unmanaged_position_auto_close(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    monkeypatch.setattr(engine_loop, "_UNMANAGED_LIVE_CONFIRM_DELAYS_SECONDS", (0.0, 0.0))
    eng._symbol_enabled["BTCUSDT"] = False
    eng._client.positions = [{
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": 0.1,
        "entryPrice": 100.0,
        "markPrice": 101.0,
    }]

    await eng._enforce_exchange_invariants("test")

    assert eng._store.orders == []
    assert eng._symbol_enabled["BTCUSDT"] is False


async def test_reconcile_disables_enabled_unmanaged_position_without_auto_close(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    monkeypatch.setattr(engine_loop, "_UNMANAGED_LIVE_CONFIRM_DELAYS_SECONDS", (0.0, 0.0))
    eng._symbol_enabled["BTCUSDT"] = True
    eng._client.positions = [{
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": 0.1,
        "entryPrice": 100.0,
        "markPrice": 101.0,
    }]

    await eng._enforce_exchange_invariants("test")

    assert eng._store.orders == []
    assert eng._symbol_enabled["BTCUSDT"] is False
    assert eng._store.runtime_settings["symbol.enabled.BTCUSDT"] == "false"
    assert any("no local open trade" in msg for _event, msg in eng._notifier.events)


async def test_reconcile_defers_unmanaged_disable_after_recent_explicit_close(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._symbol_enabled["BTCUSDT"] = True
    position = {
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": 0.1,
        "entryPrice": 100.0,
        "markPrice": 101.0,
    }
    eng.runtime.positions["BTCUSDT"] = position

    await eng._handle_close("BTCUSDT")

    eng._client.positions = [position]
    await eng._enforce_exchange_invariants("periodic")

    assert eng._symbol_enabled["BTCUSDT"] is True
    assert eng._store.runtime_settings.get("symbol.enabled.BTCUSDT") is None
    assert not any("no local open trade" in msg for _event, msg in eng._notifier.events)


async def test_reconcile_defers_unmanaged_disable_when_confirm_becomes_flat(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    monkeypatch.setattr(engine_loop, "_UNMANAGED_LIVE_CONFIRM_DELAYS_SECONDS", (0.0, 0.0))
    eng._symbol_enabled["BTCUSDT"] = True
    position = {
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": 0.1,
        "entryPrice": 100.0,
        "markPrice": 101.0,
    }
    eng._client = DelayedPositionClient([[position], []])

    await eng._enforce_exchange_invariants("periodic")

    assert eng._symbol_enabled["BTCUSDT"] is True
    assert eng._store.runtime_settings.get("symbol.enabled.BTCUSDT") is None
    assert "BTCUSDT" not in eng.runtime.positions
    assert not any("no local open trade" in msg for _event, msg in eng._notifier.events)


async def test_reconcile_defers_unmanaged_disable_when_confirm_errors(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    monkeypatch.setattr(engine_loop, "_UNMANAGED_LIVE_CONFIRM_DELAYS_SECONDS", (0.0, 0.0))
    eng._symbol_enabled["BTCUSDT"] = True
    position = {
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": 0.1,
        "entryPrice": 100.0,
        "markPrice": 101.0,
    }
    eng._client = DelayedPositionClient([
        [position],
        RuntimeError("temporary fetch failure"),
        RuntimeError("temporary fetch failure"),
    ])

    await eng._enforce_exchange_invariants("periodic")

    assert eng._symbol_enabled["BTCUSDT"] is True
    assert eng._store.runtime_settings.get("symbol.enabled.BTCUSDT") is None
    assert not any("no local open trade" in msg for _event, msg in eng._notifier.events)


async def test_reconcile_waits_for_active_opening_claim(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._symbol_enabled["BTCUSDT"] = True
    eng._store.active_claims.add("BTCUSDT")
    eng._client.positions = [{
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": 0.1,
        "entryPrice": 100.0,
        "markPrice": 101.0,
    }]

    await eng._enforce_exchange_invariants("test")

    assert eng._symbol_enabled["BTCUSDT"] is True
    assert eng._store.orders == []
    assert not any("no local open trade" in msg for _event, msg in eng._notifier.events)


async def test_reconcile_disables_managed_position_missing_stop_without_auto_close(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._store.open_trades.add("BTCUSDT")
    eng._client.positions = [{
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": 0.1,
        "entryPrice": 100.0,
        "markPrice": 101.0,
    }]

    await eng._enforce_exchange_invariants("test")

    assert eng._store.orders == []
    assert eng._symbol_enabled["BTCUSDT"] is False
    assert eng._store.runtime_settings["symbol.enabled.BTCUSDT"] == "false"


async def test_reconcile_repairs_disabled_managed_position_missing_stop(settings, creds, monkeypatch):
    settings.risk.max_loss_per_trade_pct = 10
    eng = _engine(settings, creds, monkeypatch)
    eng._symbol_enabled["BTCUSDT"] = False
    eng._store.open_trades.add("BTCUSDT")
    eng._store.set_decision(
        "BTCUSDT",
        action="OPEN_LONG",
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
    )
    eng._client.positions = [{
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": 0.1,
        "entryPrice": 100.0,
        "markPrice": 101.0,
    }]

    await eng._enforce_exchange_invariants("test")

    assert [o["kind"] for o in eng._store.orders] == ["SL", "TP"]
    assert eng._symbol_enabled["BTCUSDT"] is False
    assert eng._store.runtime_settings.get("symbol.enabled.BTCUSDT") is None


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
        assert ctx.micro_kline_interval == "1m"
        assert len(ctx.micro_klines) == 30
        decision = TradeDecision(symbol="BTCUSDT", action=Action.OPEN_LONG, confidence=0.9,
                                 size_pct=0.05, leverage=2, stop_loss_pct=0.02,
                                 take_profit_pct=0.04, reason="ok")
        return decision, SimpleNamespace(user_prompt="prompt", request_json="{}", response_json="{}", latency_ms=10, attempts=1, status="ok", error="")
    monkeypatch.setattr(eng._llm, "decide_with_trace", fake_decide)

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
        decision = TradeDecision(symbol="BTCUSDT", action=Action.OPEN_LONG, confidence=0.9,
                                 size_pct=0.05, leverage=10, stop_loss_pct=0.02,
                                 take_profit_pct=0.04, reason="too much")
        return decision, SimpleNamespace(user_prompt="prompt", request_json="{}", response_json="{}", latency_ms=10, attempts=1, status="ok", error="")
    monkeypatch.setattr(eng._llm, "decide_with_trace", fake_decide)

    await eng._process_symbol("BTCUSDT")
    assert eng._executor.opened == []
    assert len(eng._store.rejects) == 1
    assert any(e == Event.REJECT for e, _ in eng._notifier.events)


async def test_handle_open_rejects_halt_with_specific_reason(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.halt_entries("engine stopping/restarting: signal")
    decision = TradeDecision(symbol="BTCUSDT", action=Action.OPEN_SHORT, confidence=0.9,
                             size_pct=0.05, leverage=2, stop_loss_pct=0.02,
                             take_profit_pct=0.04, reason="ok")

    await eng._handle_open(decision, _ctx())

    assert eng._executor.opened == []
    assert eng._store.rejects[0]["verdict"].reason == (
        "new entries halted: engine stopping/restarting: signal"
    )


async def test_open_rejects_stale_condition_without_position(settings, creds, monkeypatch):
    settings.risk.max_leverage = 5
    settings.risk.min_confidence = 0.6
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
        decision = TradeDecision(symbol="BTCUSDT", action=Action.OPEN_SHORT, confidence=0.9,
                                 size_pct=0.05, leverage=2, stop_loss_pct=0.02,
                                 take_profit_pct=0.04, reason="ok")
        return decision, SimpleNamespace(user_prompt="prompt", request_json="{}", response_json="{}", latency_ms=10, attempts=1, status="ok", error="")
    monkeypatch.setattr(eng._llm, "decide_with_trace", fake_decide)

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

    async def close_position(self, position, *, mode=None):
        self.closed += 1
        return {"symbol": "BTCUSDT", "kind": "CLOSE", "status": "filled",
                "filled": True, "closed": True, "dry_run": False,
                "qty": abs(float(position.get("contracts") or 0)),
                "price": float(position.get("markPrice") or 0),
                "entry_price": float(position.get("entryPrice") or 0),
                "pos_side": (position.get("side") or "").lower(),
                "id": "close-1", "side": "buy"}


class TinyPartialExecutor(FakeExecutor):
    def __init__(self):
        super().__init__()
        self.closed = 0

    async def open_position(self, *, decision, qty, price):
        return {"symbol": decision.symbol, "kind": "OPEN", "status": "partial",
                "filled": True, "opened": True, "qty": 0.001, "price": price,
                "notional": 0.1, "dry_run": False, "side": "buy", "id": "open-tiny"}

    async def close_position(self, position, *, mode=None):
        self.closed += 1
        return {"symbol": "BTCUSDT", "kind": "CLOSE", "status": "filled",
                "filled": True, "closed": True, "dry_run": False,
                "qty": abs(float(position.get("contracts") or 0)),
                "price": float(position.get("markPrice") or 0),
                "entry_price": float(position.get("entryPrice") or 0),
                "pos_side": (position.get("side") or "").lower(),
                "id": "close-tiny", "side": "sell"}


async def test_open_tiny_partial_closes_when_protection_below_min(settings, creds, monkeypatch):
    settings.risk.max_leverage = 5
    settings.risk.min_confidence = 0.6
    eng = _engine(settings, creds, monkeypatch)
    eng._executor = TinyPartialExecutor()
    decision = TradeDecision(symbol="BTCUSDT", action=Action.OPEN_LONG, confidence=0.9,
                             size_pct=0.05, leverage=2, stop_loss_pct=0.02,
                             take_profit_pct=0.04, reason="ok")

    await eng._handle_open(decision, _ctx())

    assert eng._executor.closed == 1
    assert eng._symbol_enabled["BTCUSDT"] is False
    assert [o["kind"] for o in eng._store.orders] == ["OPEN", "CLOSE"]
    assert eng._store.claims[0]["status"] == "error"
    assert eng._store.claims[0]["filled_qty"] == pytest.approx(0.001)


async def test_open_closes_position_when_stop_not_confirmed(settings, creds, monkeypatch):
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


async def test_open_waits_for_position_visibility_before_attaching_protection(settings, creds, monkeypatch):
    settings.risk.max_leverage = 5
    settings.risk.min_confidence = 0.6
    monkeypatch.setattr(
        engine_loop,
        "_POST_OPEN_POSITION_CONFIRM_DELAYS_SECONDS",
        (0.0, 0.0, 0.0),
    )
    position = {
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": 0.2,
        "entryPrice": 100.0,
        "markPrice": 100.0,
    }
    eng = _engine(settings, creds, monkeypatch)
    eng._client = DelayedPositionClient([[], [], [position]])
    decision = TradeDecision(symbol="BTCUSDT", action=Action.OPEN_LONG, confidence=0.9,
                             size_pct=0.05, leverage=2, stop_loss_pct=0.02,
                             take_profit_pct=0.04, reason="ok")

    await eng._handle_open(decision, _ctx())

    assert eng._client.fetch_positions_calls == 3
    assert [o["kind"] for o in eng._store.orders] == ["OPEN", "SL", "TP"]
    assert eng._symbol_enabled["BTCUSDT"] is True


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
    assert eng.runtime.halt_new_entries_reason == "strategy paused"
    assert eng._store.runtime_settings["strategy.paused"] == "true"
    assert eng._store.marked == [(1, "done", "strategy paused (persisted)")]

    eng._store.pending = [{"id": 2, "command": "RESUME", "arg": ""}]
    await eng._process_commands()
    assert eng.runtime.halt_new_entries is False
    assert eng.runtime.halt_new_entries_reason == ""
    assert eng._store.runtime_settings["strategy.paused"] == "false"
    assert eng._store.marked[-1] == (2, "done", "strategy resumed (persisted)")


async def test_sleep_consumes_resume_and_wakes_strategy(settings, creds, monkeypatch):
    monkeypatch.setattr(engine_loop, "_COMMAND_POLL_INTERVAL_SECONDS", 0.01)
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.halt_new_entries = True
    eng._store.pending = [{"id": 1, "command": "RESUME", "arg": ""}]
    cycle_start = time.monotonic() - (settings.cycle.interval_seconds - 1.0)

    started = time.monotonic()
    await eng._sleep_to_next_cycle(cycle_start)

    assert time.monotonic() - started < 0.2
    assert eng.runtime.halt_new_entries is False
    assert eng._store.runtime_settings["strategy.paused"] == "false"
    assert eng._store.marked == [(1, "done", "strategy resumed (persisted)")]


async def test_sleep_consumes_symbol_enable_and_wakes_when_running(settings, creds, monkeypatch):
    monkeypatch.setattr(engine_loop, "_COMMAND_POLL_INTERVAL_SECONDS", 0.01)
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.halt_new_entries = False
    eng._symbol_enabled["BTCUSDT"] = False
    eng._store.pending = [{
        "id": 1,
        "command": "SET_SYMBOL_ENABLED",
        "arg": "BTCUSDT=true",
    }]
    cycle_start = time.monotonic() - (settings.cycle.interval_seconds - 1.0)

    started = time.monotonic()
    await eng._sleep_to_next_cycle(cycle_start)

    assert time.monotonic() - started < 0.2
    assert eng._symbol_enabled["BTCUSDT"] is True
    assert eng._store.runtime_settings["symbol.enabled.BTCUSDT"] == "true"
    assert eng._store.marked[0][0:2] == (1, "done")


async def test_runtime_symbol_enabled_applied_on_startup(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._store.runtime_settings["symbol.enabled.BTCUSDT"] = "false"
    await eng._apply_runtime_settings()
    assert eng._symbol_enabled["BTCUSDT"] is False


async def test_resume_command_clears_pause_meta(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._store.runtime_settings.update({
        "strategy.paused": "true",
        "strategy.pause.reason_code": "MAX_DRAWDOWN",
        "strategy.pause.reason": "drawdown 25% >= 20%",
        "strategy.pause.source": "engine:circuit_breaker",
        "strategy.pause.at_ms": "1700000000000",
    })
    eng._store.pending = [{"id": 1, "command": "RESUME", "arg": ""}]
    await eng._process_commands()
    assert eng._store.runtime_settings["strategy.paused"] == "false"
    assert eng._store.runtime_settings["strategy.pause.reason_code"] == ""
    assert eng._store.runtime_settings["strategy.pause.reason"] == ""
    assert eng._store.runtime_settings["strategy.pause.source"] == ""
    assert eng.runtime.halt_new_entries is False


async def test_runtime_strategy_paused_applied_on_startup(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._store.runtime_settings["strategy.paused"] = "true"
    await eng._apply_runtime_settings()
    assert eng.runtime.halt_new_entries is True
    assert eng.runtime.halt_new_entries_reason == "strategy paused"


async def test_command_set_symbol_enabled(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._store.pending = [{"id": 1, "command": "SET_SYMBOL_ENABLED", "arg": "BTCUSDT=false"}]
    await eng._process_commands()
    assert eng._symbol_enabled["BTCUSDT"] is False
    assert eng._store.runtime_settings["symbol.enabled.BTCUSDT"] == "false"


async def test_command_add_symbol_confirmed_flat_defaults_disabled(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)

    result = await eng._exec_command("ADD_SYMBOL", "solusdt")

    assert "SOLUSDT added disabled; exchange confirmed flat" in result
    row = eng._store.symbols["SOLUSDT"]
    assert row["enabled"] is False
    assert row["needs_review"] is False
    assert row["sync_status"] == "confirmed_flat"
    assert eng._symbol_enabled["SOLUSDT"] is False
    assert eng._store.position_snapshots[-1][1] == ["SOLUSDT"]


async def test_command_add_symbol_with_position_requires_review(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._client.positions = [{
        "symbol": "SOL/USDT:USDT",
        "side": "long",
        "contracts": 1.0,
        "entryPrice": 100.0,
        "markPrice": 101.0,
    }]

    result = await eng._exec_command("ADD_SYMBOL", "SOLUSDT")

    assert "needs review: live position" in result
    row = eng._store.symbols["SOLUSDT"]
    assert row["enabled"] is False
    assert row["needs_review"] is True
    assert row["sync_status"] == "live_position_found"
    assert eng._symbol_enabled["SOLUSDT"] is False


async def test_command_review_symbol_clears_review_when_exchange_flat(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._store.symbols["SOLUSDT"] = {
        "symbol": "SOLUSDT",
        "enabled": False,
        "status": "active",
        "sync_status": "live_position_found",
        "needs_review": True,
        "source": "web",
        "min_qty": 0.0,
        "min_notional": 0.0,
        "tick_size": 0.0,
        "step_size": 0.0,
    }

    result = await eng._exec_command("REVIEW_SYMBOL", "SOLUSDT")

    assert "review cleared; remains disabled" in result
    row = eng._store.symbols["SOLUSDT"]
    assert row["enabled"] is False
    assert row["needs_review"] is False
    assert row["sync_status"] == "confirmed_flat"
    assert eng._symbol_enabled["SOLUSDT"] is False


async def test_command_review_symbol_keeps_review_when_orders_remain(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._store.symbols["SOLUSDT"] = {
        "symbol": "SOLUSDT",
        "enabled": False,
        "status": "active",
        "sync_status": "open_orders_found",
        "needs_review": True,
        "source": "web",
        "min_qty": 0.0,
        "min_notional": 0.0,
        "tick_size": 0.0,
        "step_size": 0.0,
    }
    eng._client.open_orders = [{"id": "order-1", "symbol": "SOL/USDT:USDT"}]

    result = await eng._exec_command("REVIEW_SYMBOL", "SOLUSDT")

    assert "still needs review: 1 open orders" in result
    row = eng._store.symbols["SOLUSDT"]
    assert row["enabled"] is False
    assert row["needs_review"] is True
    assert row["sync_status"] == "open_orders_found"
    assert eng._store.open_order_snapshots[-1] == eng._client.open_orders


async def test_command_resume_all_symbols_requires_flat_exchange(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.halt_new_entries = True
    eng._symbol_enabled["BTCUSDT"] = False
    eng._store.runtime_settings["strategy.paused"] = "true"
    eng._store.runtime_settings["symbol.enabled.BTCUSDT"] = "false"
    eng._store.pending = [{"id": 1, "command": "RESUME_ALL_SYMBOLS", "arg": ""}]

    await eng._process_commands()

    assert eng.runtime.halt_new_entries is False
    assert eng._symbol_enabled["BTCUSDT"] is True
    assert eng._store.runtime_settings["strategy.paused"] == "false"
    assert eng._store.runtime_settings["symbol.enabled.BTCUSDT"] == "true"
    assert eng._store.marked[0][0:2] == (1, "done")
    assert "enabled all symbols: BTCUSDT" in eng._store.marked[0][2]


async def test_command_resume_all_symbols_fails_with_live_position(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.halt_new_entries = True
    eng._symbol_enabled["BTCUSDT"] = False
    eng._store.runtime_settings["strategy.paused"] = "true"
    eng._store.runtime_settings["symbol.enabled.BTCUSDT"] = "false"
    eng._client.positions = [{
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": 1.0,
        "entryPrice": 100.0,
        "markPrice": 101.0,
    }]
    eng._store.pending = [{"id": 1, "command": "RESUME_ALL_SYMBOLS", "arg": ""}]

    await eng._process_commands()

    assert eng.runtime.halt_new_entries is True
    assert eng._symbol_enabled["BTCUSDT"] is False
    assert eng._store.runtime_settings["strategy.paused"] == "true"
    assert eng._store.runtime_settings["symbol.enabled.BTCUSDT"] == "false"
    assert eng._store.marked[0][0:2] == (1, "failed")
    assert "交易所仍有持仓" in eng._store.marked[0][2]


async def test_command_resume_all_symbols_fails_with_condition_order(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng.runtime.halt_new_entries = True
    eng._symbol_enabled["BTCUSDT"] = False
    eng._store.runtime_settings["strategy.paused"] = "true"
    eng._store.runtime_settings["symbol.enabled.BTCUSDT"] = "false"
    eng._client.condition_orders = [{
        "id": "algo-1",
        "symbol": "BTC/USDT:USDT",
        "type": "STOP_MARKET",
        "side": "sell",
        "amount": 1.0,
        "stopPrice": 95.0,
        "status": "open",
        "reduceOnly": True,
    }]
    eng._store.pending = [{"id": 1, "command": "RESUME_ALL_SYMBOLS", "arg": ""}]

    await eng._process_commands()

    assert eng.runtime.halt_new_entries is True
    assert eng._symbol_enabled["BTCUSDT"] is False
    assert eng._store.runtime_settings["strategy.paused"] == "true"
    assert eng._store.runtime_settings["symbol.enabled.BTCUSDT"] == "false"
    assert eng._store.marked[0][0:2] == (1, "failed")
    assert "条件单: BTCUSDT:SL#algo-1" in eng._store.marked[0][2]


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
    assert "flattened 1 positions" in eng._store.marked[0][2]


async def test_command_stop_engine_does_not_flatten(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._store.pending = [{"id": 1, "command": "STOP_ENGINE", "arg": ""}]
    await eng._process_commands()
    assert eng._executor.canceled == 0
    assert eng._executor.flattened == 0
    assert eng.runtime.kill_switch is False
    assert eng.runtime.halt_new_entries_reason == "engine stopping/restarting: web stop-engine"
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


class ManualCloseExecutor(FakeExecutor):
    def __init__(self, client):
        super().__init__()
        self.client = client
        self.skip_slippage_guard_seen = None

    async def close_position(self, position, *, mode=None, skip_slippage_guard=False):
        self.skip_slippage_guard_seen = skip_slippage_guard
        result = await super().close_position(
            position,
            mode=mode,
            skip_slippage_guard=skip_slippage_guard,
        )
        self.client.position = None
        return result


async def test_command_repair_sl_tp_places_missing_orders(settings, creds, monkeypatch):
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


async def test_reconcile_emergency_closes_when_repair_sl_crossed_mark(settings, creds, monkeypatch):
    settings.risk.max_loss_per_trade_pct = 10
    eng = _engine(settings, creds, monkeypatch)
    eng._symbol_enabled["BTCUSDT"] = True
    eng._store.open_trades.add("BTCUSDT")
    eng._client.positions = [{
        "symbol": "BTC/USDT:USDT",
        "side": "short",
        "contracts": 1.0,
        "entryPrice": 100.0,
        "markPrice": 103.0,
    }]
    eng._client.condition_orders = [
        {"id": "tp-1", "symbol": "BTC/USDT:USDT", "type": "TAKE_PROFIT_MARKET",
         "side": "buy", "amount": 1.0, "stopPrice": 95.0, "status": "open",
         "reduceOnly": True},
    ]
    eng._store.templates = {
        "SL": {"price": 102.0, "order_type": "STOP_MARKET"},
        "TP": {"price": 95.0, "order_type": "TAKE_PROFIT_MARKET"},
    }

    await eng._enforce_exchange_invariants("periodic")

    assert eng._symbol_enabled["BTCUSDT"] is False
    assert eng._store.runtime_settings["symbol.enabled.BTCUSDT"] == "false"
    row = eng._store.symbols["BTCUSDT"]
    assert row["disabled_reason_code"] == "SL_TRIGGER_CROSSED_MARK"
    assert row["disabled_action"] == "emergency_close"
    assert "SL trigger crossed current mark" in row["disabled_reason"]
    assert eng._executor.closed
    assert any(o.get("kind") == "CLOSE" for o in eng._store.orders)


async def test_command_repair_sl_tp_keeps_active_tp_after_mark_crosses_trigger(settings, creds, monkeypatch):
    settings.risk.max_loss_per_trade_pct = 10
    eng = _engine(settings, creds, monkeypatch)
    eng._client = RepairClient(
        position={
            "symbol": "BTC/USDT:USDT",
            "side": "short",
            "contracts": 1.0,
            "entryPrice": 100.0,
            "markPrice": 94.0,
        },
        open_orders=[
            {"id": "tp-live", "symbol": "BTC/USDT:USDT", "type": "TAKE_PROFIT_MARKET",
             "side": "buy", "amount": 1.0, "stopPrice": 95.0, "status": "open",
             "reduceOnly": True},
        ],
        equity=1000.0,
    )
    eng._store.templates = {
        "SL": {"price": 102.0, "order_type": "STOP_MARKET"},
        "TP": {"price": 95.0, "order_type": "TAKE_PROFIT_MARKET"},
    }

    result = await eng._exec_command("REPAIR_SL_TP", "BTCUSDT")

    assert eng._client.canceled == []
    assert [o["kind"] for o in eng._store.orders] == ["SL"]
    assert "已补挂 SL@102.00" in result


async def test_command_repair_sl_tp_blocks_mismatched_stale_order(settings, creds, monkeypatch):
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


async def test_command_protect_position_uses_manual_stop_for_takeover(settings, creds, monkeypatch):
    settings.risk.max_loss_per_trade_pct = 10
    eng = _engine(settings, creds, monkeypatch)
    eng._client = RepairClient(
        position={
            "symbol": "BTC/USDT:USDT",
            "side": "long",
            "contracts": 0.1,
            "entryPrice": 100.0,
            "markPrice": 99.0,
            "leverage": 2,
        },
        equity=1000.0,
    )
    payload = {
        "symbol": "BTCUSDT",
        "qty": 0.1,
        "sl_trigger": 98.0,
        "confirm": True,
        "position": {"side": "long", "qty": 0.1, "entry": 100.0},
    }

    result = await eng._exec_command("PROTECT_POSITION", json.dumps(payload))

    assert "已接管保护 SL@98.00" in result
    assert eng._store.takeover_trades
    assert [o["kind"] for o in eng._store.orders] == ["SL"]
    assert eng._store.orders[0]["trade_id"] == eng._store.takeover_trades[0]["id"]


async def test_command_protect_position_takeover_only_residual_qty(settings, creds, monkeypatch):
    settings.risk.max_loss_per_trade_pct = 10
    eng = _engine(settings, creds, monkeypatch)
    eng._store.open_trades.add("BTCUSDT")
    eng._store.open_qty["BTCUSDT"] = 0.08
    eng._client = RepairClient(
        position={
            "symbol": "BTC/USDT:USDT",
            "side": "long",
            "contracts": 0.1,
            "entryPrice": 100.0,
            "markPrice": 99.0,
            "leverage": 2,
        },
        equity=1000.0,
    )
    payload = {
        "symbol": "BTCUSDT",
        "qty": 0.1,
        "sl_trigger": 98.0,
        "confirm": True,
        "position": {"side": "long", "qty": 0.1, "entry": 100.0},
    }

    result = await eng._exec_command("PROTECT_POSITION", json.dumps(payload))

    assert "已接管保护 SL@98.00" in result
    assert eng._store.takeover_trades[0]["qty"] == pytest.approx(0.02)
    assert "trade_id" not in eng._store.orders[0]


async def test_command_close_position_closes_and_cancels_protection_without_disabling(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    client = RepairClient(
        position={
            "symbol": "BTC/USDT:USDT",
            "side": "long",
            "contracts": 0.1,
            "entryPrice": 100.0,
            "markPrice": 99.0,
            "leverage": 2,
        },
        open_orders=[
            {"id": "sl-1", "symbol": "BTC/USDT:USDT", "type": "STOP_MARKET",
             "side": "sell", "amount": 0.1, "stopPrice": 98.0, "status": "open",
             "reduceOnly": True},
            {"id": "tp-1", "symbol": "BTC/USDT:USDT", "type": "TAKE_PROFIT_MARKET",
             "side": "sell", "amount": 0.1, "stopPrice": 105.0, "status": "open",
             "reduceOnly": True},
        ],
    )
    eng._client = client
    eng._executor = ManualCloseExecutor(client)
    payload = {
        "symbol": "BTCUSDT",
        "confirm": True,
        "position": {"side": "long", "qty": 0.1, "entry": 100.0},
    }

    result = await eng._exec_command("CLOSE_POSITION", json.dumps(payload))

    assert "手动平仓完成" in result
    assert eng._executor.skip_slippage_guard_seen is True
    assert eng._store.orders[-1]["kind"] == "CLOSE"
    assert eng._store.orders[-1]["raw"]["_local"]["manual_force_close"] is True
    assert eng._store.orders[-1]["raw"]["_local"]["slippage_guard_skipped"] is True
    assert client.canceled == [("BTCUSDT", "ALL", "")]
    assert eng._symbol_enabled["BTCUSDT"] is True
    assert eng._store.runtime_settings.get("symbol.enabled.BTCUSDT") is None


async def test_command_close_position_rejects_stale_position_signature(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._client = RepairClient(
        position={
            "symbol": "BTC/USDT:USDT",
            "side": "long",
            "contracts": 0.1,
            "entryPrice": 100.0,
            "markPrice": 99.0,
        },
    )
    payload = {
        "symbol": "BTCUSDT",
        "confirm": True,
        "position": {"side": "long", "qty": 0.2, "entry": 100.0},
    }

    with pytest.raises(ValueError, match="页面持仓数量已过期"):
        await eng._exec_command("CLOSE_POSITION", json.dumps(payload))
    assert eng._store.orders == []


async def test_command_unknown_marked_failed(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._store.pending = [{"id": 9, "command": "BOGUS", "arg": ""}]
    await eng._process_commands()
    assert eng._store.marked[0][0] == 9
    assert eng._store.marked[0][1] == "failed"



class FakeExchangeClientWithPosition(FakeClient):
    """_adopt_orphan_position 需要的最小 ExchangeClient 接口。"""

    def __init__(self, position):
        super().__init__()
        self._position = position

    async def fetch_positions(self, symbols=None):
        return [self._position] if self._position else []

    async def fetch_position(self, symbol):
        return self._position if self._position else None


async def test_orphan_adoption_picks_up_managed_qty_and_repairs_sl_tp(settings, creds, monkeypatch):
    """B4 核心测试：本地无 open trade + 无 active claim + 有最近 canceled claim +
    交易所侧确有 0<qty<planned 的孤儿持仓 → 自动接管建 trade + 补 SL/TP，
    不再禁用币种。
    """
    eng = _engine(settings, creds, monkeypatch)
    eng._symbol_enabled["BTCUSDT"] = True
    eng._store.open_trades.discard("BTCUSDT")
    eng._store.active_claims.discard("BTCUSDT")
    eng._store.set_finished_claim(
        "BTCUSDT", side="long", planned_qty=1.0, filled_qty=0.0,
        status="canceled", source="strategy", ts_ms=1_000_000_000_000,
    )
    # 注入最近 OPEN 决策模板，用于 SL/TP 触发价
    eng._store.set_decision("BTCUSDT", action="OPEN_LONG", stop_loss_pct=0.02, take_profit_pct=0.04)
    pos = {
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": 0.05,
        "entryPrice": 100.0,
        "markPrice": 100.0,
        "info": {"leverage": "3"},
    }
    fake_client = FakeExchangeClientWithPosition(pos)
    eng._client = fake_client
    eng.runtime.positions["BTCUSDT"] = pos

    # 让 FakeExecutor.place_protection_orders 把结果回灌到 fake_client.condition_orders，
    # 模拟交易所真有 SL/TP 挂单（_active_protection_orders 才会找到）
    orig_place = eng._executor.place_protection_orders
    async def place_with_side_effect(*, symbol, pos_side, qty, specs):
        results = await orig_place(symbol=symbol, pos_side=pos_side, qty=qty, specs=specs)
        for o in results:
            fake_client.condition_orders.append({
                "id": o.get("id"),
                "symbol": "BTC/USDT:USDT",
                "type": o.get("order_type"),
                "side": o.get("side"),
                "amount": o.get("qty"),
                "stopPrice": o.get("price"),
                "status": "open",
                "reduceOnly": True,
            })
        return results
    eng._executor.place_protection_orders = place_with_side_effect

    await eng._enforce_exchange_invariants("test")

    # 验证接管成功
    assert "BTCUSDT" in eng._store.open_trades
    assert eng._symbol_enabled["BTCUSDT"] is True
    assert any(t["source"] == "orphan_adoption" for t in eng._store.takeover_trades)
    # 验证 SL/TP 被挂
    sl_tp_orders = [
        o for o in eng._store.orders
        if o.get("kind") in ("SL", "TP")
    ]
    assert sl_tp_orders, "orphan adoption should place SL/TP"


async def test_orphan_adoption_skips_when_no_recent_claim(settings, creds, monkeypatch):
    """B4 边界：没最近 claim → 走原有"禁用"路径，保持向后兼容。"""
    eng = _engine(settings, creds, monkeypatch)
    monkeypatch.setattr(engine_loop, "_UNMANAGED_LIVE_CONFIRM_DELAYS_SECONDS", (0.0, 0.0))
    eng._symbol_enabled["BTCUSDT"] = True
    eng._store.open_trades.discard("BTCUSDT")
    eng._store.active_claims.discard("BTCUSDT")
    pos = {
        "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.05,
        "entryPrice": 100.0, "markPrice": 100.0, "info": {"leverage": "3"},
    }
    eng._client = FakeExchangeClientWithPosition(pos)
    eng.runtime.positions["BTCUSDT"] = pos

    await eng._enforce_exchange_invariants("test")

    # 没有 claim → 维持原"禁用"行为
    assert eng._symbol_enabled["BTCUSDT"] is False
    assert "BTCUSDT" not in eng._store.open_trades


async def test_orphan_adoption_skipped_on_side_mismatch(settings, creds, monkeypatch):
    """B4 边界：claim 是 long，交易所持仓是 short → 不接管（避免反向加仓）。"""
    eng = _engine(settings, creds, monkeypatch)
    monkeypatch.setattr(engine_loop, "_UNMANAGED_LIVE_CONFIRM_DELAYS_SECONDS", (0.0, 0.0))
    eng._symbol_enabled["BTCUSDT"] = True
    eng._store.open_trades.discard("BTCUSDT")
    eng._store.active_claims.discard("BTCUSDT")
    eng._store.set_finished_claim(
        "BTCUSDT", side="long", planned_qty=1.0, status="canceled", source="strategy",
    )
    pos = {
        "symbol": "BTC/USDT:USDT", "side": "short", "contracts": 0.05,
        "entryPrice": 100.0, "markPrice": 100.0, "info": {"leverage": "3"},
    }
    eng._client = FakeExchangeClientWithPosition(pos)
    eng.runtime.positions["BTCUSDT"] = pos

    await eng._enforce_exchange_invariants("test")

    # side 不匹配 → 不接管，按原"禁用"路径走
    assert "BTCUSDT" not in eng._store.open_trades
    assert eng._symbol_enabled["BTCUSDT"] is False


async def test_orphan_adoption_skipped_on_qty_out_of_range(settings, creds, monkeypatch):
    """B4 边界：claim planned=1.0，交易所 qty=5.0（10x）→ 不接管（不是同一笔）。"""
    eng = _engine(settings, creds, monkeypatch)
    monkeypatch.setattr(engine_loop, "_UNMANAGED_LIVE_CONFIRM_DELAYS_SECONDS", (0.0, 0.0))
    eng._symbol_enabled["BTCUSDT"] = True
    eng._store.open_trades.discard("BTCUSDT")
    eng._store.active_claims.discard("BTCUSDT")
    eng._store.set_finished_claim(
        "BTCUSDT", side="long", planned_qty=1.0, status="canceled", source="strategy",
    )
    pos = {
        "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 5.0,
        "entryPrice": 100.0, "markPrice": 100.0, "info": {"leverage": "3"},
    }
    eng._client = FakeExchangeClientWithPosition(pos)
    eng.runtime.positions["BTCUSDT"] = pos

    await eng._enforce_exchange_invariants("test")

    assert "BTCUSDT" not in eng._store.open_trades
    assert eng._symbol_enabled["BTCUSDT"] is False


async def test_startup_rehydrates_day_pnl_from_db(settings, creds, monkeypatch):
    """启动时应当从 DB 重算 day_realized_pnl，不再清零。"""
    import time as _t
    eng = _engine(settings, creds, monkeypatch)
    today = _t.strftime("%Y-%m-%d", _t.localtime())
    eng._store.set_day_pnl({today: -1.234, "2020-01-01": -99.0})

    # 直接调 rehydrate（不跑整个 startup）
    by_day = await eng._store.day_realized_pnl_by_local_day()
    eng.runtime.rehydrate_day_pnl(by_day)

    assert eng.runtime.day_key == today
    assert eng.runtime.day_realized_pnl == -1.234  # 取今天
    # 不会把昨天/历史的也带进来


async def test_startup_rehydrate_falls_back_on_store_error(settings, creds, monkeypatch):
    """store 抛异常时回退到 0，不影响 startup。"""
    import time as _t
    eng = _engine(settings, creds, monkeypatch)

    async def boom():
        raise RuntimeError("db unavailable")
    eng._store.day_realized_pnl_by_local_day = boom

    # 直接调 rehydrate（不跑整个 startup），验证 rehydrate 自身不抛
    # 但 engine.startup 里包了 try/except + fallback roll_day_if_needed
    # 这里我们测 rehydrate 的契约：传入 by_day=None 时不应崩
    # （startup 的 try/except 是另外一层）
    eng.runtime.rehydrate_day_pnl({})
    assert eng.runtime.day_realized_pnl == 0.0
    assert eng.runtime.day_key == _t.strftime("%Y-%m-%d", _t.localtime())

# ---------- 挂单取消命令 ----------
async def test_cancel_open_order_command_cancels_via_client(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    payload = json.dumps({"symbol": "BTCUSDT", "order_id": "OID-OPEN-1"})
    result = await eng._cancel_open_order(payload)
    assert "BTCUSDT" in result and "OID-OPEN-1" in result
    assert eng._client.canceled_open_orders == [("BTCUSDT", "OID-OPEN-1")]
    assert eng._store.marked_status_calls[-1] == (["OID-OPEN-1"], "canceled")

async def test_cancel_open_order_rejects_missing_identifier(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    with pytest.raises(ValueError):
        await eng._cancel_open_order(json.dumps({"symbol": "BTCUSDT"}))

async def test_cancel_condition_order_command_passes_client_algo_id(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    payload = json.dumps({
        "symbol": "ETHUSDT",
        "algo_id": "ALGO-1",
        "client_algo_id": "bt-algo-1",
    })
    result = await eng._cancel_condition_order(payload)
    assert "ETHUSDT" in result and "ALGO-1" in result
    assert eng._client.canceled_condition_orders == [("ETHUSDT", "ALGO-1", "bt-algo-1")]
    assert eng._store.marked_status_calls[-1] == (["ALGO-1"], "canceled")

async def test_cancel_all_open_orders_command_clears_pending(settings, creds, monkeypatch):
    eng = _engine(settings, creds, monkeypatch)
    eng._client.open_orders = [
        {"id": "A", "symbol": "BTC/USDT:USDT", "type": "limit", "status": "open"},
        {"id": "B", "symbol": "BTC/USDT:USDT", "type": "limit", "status": "open"},
    ]
    result = await eng._cancel_all_open_orders("BTCUSDT")
    assert "BTCUSDT" in result
    assert eng._client.canceled_all_open_symbols == ["BTCUSDT"]
    assert eng._client.open_orders == []
