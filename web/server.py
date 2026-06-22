"""Web 后端：FastAPI 只读看板 + WebSocket 推送 + 受控操作端点。

安全与解耦原则：
- 独立进程，与交易主进程分离；只读 SQLite（status.py）+ 只读交易所行情。
- 操作类命令（Kill Switch / 暂停 / 币种开关等）只写 control_commands 表，
  由交易进程快速消费执行；web 绝不直接碰交易所下单。
- 全站 HTTP Basic Auth；WS 握手复用同源 Basic 凭证。

启动：
    python -m web.server          # 读 config.yaml + .env，默认监听 127.0.0.1:8000
凭据来自 .env：WEB_USER / WEB_PASSWORD（缺省 admin / 见日志告警）。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field

from src.config.loader import load_config
from src.exchange.client import ExchangeClient
from src.exchange.positions import normalize_symbol
from src.features.indicators import compute_snapshot
from src.engine.settings import (
    RUNTIME_ENGINE_KEY,
    RUNTIME_ENGINE_VERSION_KEY,
    decode_engine,
    engine_defaults_from_settings,
    engine_public,
    validate_engine_payload,
)
from src.execution.settings import (
    RUNTIME_EXECUTION_KEY,
    RUNTIME_EXECUTION_VERSION_KEY,
    decode_execution,
    execution_defaults_from_settings,
    execution_fixed_public,
    execution_public,
    validate_execution_payload,
)
from src.risk.settings import (
    RUNTIME_RISK_KEY,
    RUNTIME_RISK_VERSION_KEY,
    decode_risk,
    risk_public,
    validate_risk_payload,
)
from src.store.repo import Store
from src.reconcile.service import BinanceTradeReconciler, ReconcileError
from web import status as st
from web.market_feed import MarketFeedRegistry

# ---------- 配置与全局 ----------
_settings, _creds = load_config(
    os.environ.get("BINANCE_CONFIG", "config.yaml"),
    os.environ.get("BINANCE_ENV", ".env"),
)
_DB = _settings.storage.db_path
_WEB_USER = os.environ.get("WEB_USER", "admin")
_WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
_SESSION_COOKIE = f"binance_trade_session_{_settings.mode.value}"
_SESSION_TTL_SECONDS = int(os.environ.get("WEB_SESSION_TTL_SECONDS", str(7 * 24 * 3600)))
_SESSION_SECRET = os.environ.get("WEB_SESSION_SECRET", "")

app = FastAPI(title="binance-trade dashboard", docs_url=None, redoc_url=None)
_security = HTTPBasic(auto_error=False)

# 行情数据源注册表：mainnet/testnet 双源（REST + WS），供看板行情用
_feeds = MarketFeedRegistry()
# 默认行情源跟随交易模式；需要单独看主网时可用 WEB_MARKET_SOURCE=mainnet 覆盖。
_SOURCE_ENV = os.environ.get("WEB_MARKET_SOURCE", _settings.mode.value).lower()
_DEFAULT_SOURCE = _SOURCE_ENV if _SOURCE_ENV in ("mainnet", "testnet") else _settings.mode.value
# 控制命令写入用的 Store（懒加载）
_store: Store | None = None
_confirmations: dict[str, dict[str, Any]] = {}
_MAINNET_CONFIRM_TTL_SECONDS = 120
_reconcile_lock = asyncio.Lock()


def _ws_auth_cookie_value() -> str:
    if not _WEB_PASSWORD:
        return ""
    return hashlib.sha256(f"{_WEB_USER}:{_WEB_PASSWORD}".encode()).hexdigest()


def _session_secret() -> bytes:
    secret = _SESSION_SECRET or f"{_settings.mode.value}:{_WEB_USER}:{_WEB_PASSWORD}"
    return hashlib.sha256(secret.encode()).digest()


def _session_cookie_value(username: str, expires_at: int | None = None) -> str:
    expires = int(expires_at or (time.time() + _SESSION_TTL_SECONDS))
    payload = f"{username}|{expires}"
    sig = hmac.new(_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    import base64
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def _session_user_from_cookie(value: str) -> str:
    if not value or not _WEB_PASSWORD:
        return ""
    try:
        import base64
        username, expires_raw, sig = base64.urlsafe_b64decode(value.encode()).decode().rsplit("|", 2)
        expires = int(expires_raw)
    except Exception:
        return ""
    if expires < int(time.time()):
        return ""
    payload = f"{username}|{expires}"
    expected = hmac.new(_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not secrets.compare_digest(sig, expected):
        return ""
    if not secrets.compare_digest(username, _WEB_USER):
        return ""
    return username


def _cookie_secure() -> bool:
    return os.environ.get("WEB_COOKIE_SECURE", "true").lower() in ("1", "true", "yes")


def _set_auth_cookies(response: Response, username: str) -> None:
    response.set_cookie(
        _SESSION_COOKIE,
        _session_cookie_value(username),
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
        max_age=_SESSION_TTL_SECONDS,
    )
    response.set_cookie(
        "binance_trade_ws",
        _ws_auth_cookie_value(),
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
        max_age=_SESSION_TTL_SECONDS,
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(_SESSION_COOKIE, samesite="lax", secure=_cookie_secure())
    response.delete_cookie("binance_trade_ws", samesite="lax", secure=_cookie_secure())


def _valid_basic(credentials: HTTPBasicCredentials | None) -> str:
    if credentials is None:
        return ""
    ok_user = secrets.compare_digest(credentials.username, _WEB_USER)
    ok_pass = secrets.compare_digest(credentials.password, _WEB_PASSWORD)
    return credentials.username if ok_user and ok_pass else ""


def _check_auth(
    request: Request,
    response: Response,
    credentials: HTTPBasicCredentials | None = Depends(_security),
) -> str:
    """Session cookie first; Basic Auth remains as a compatibility fallback."""
    if not _WEB_PASSWORD:
        # 未配置密码 → 拒绝一切访问，避免裸奔
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WEB_PASSWORD 未配置，拒绝访问。请在 .env 设置 WEB_USER/WEB_PASSWORD。",
        )
    session_user = _session_user_from_cookie(request.cookies.get(_SESSION_COOKIE, ""))
    if session_user:
        _set_auth_cookies(response, session_user)
        return session_user
    basic_user = _valid_basic(credentials)
    if basic_user:
        _set_auth_cookies(response, basic_user)
        return basic_user
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="认证失败")


async def _get_store() -> Store:
    global _store
    if _store is None:
        _store = Store(_DB)
        await _store.connect()
        await _store.sync_config_symbols(_settings.symbols)
    return _store


def _parse_bool_setting(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class _LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


@app.post("/api/auth/login")
async def api_auth_login(body: _LoginRequest, response: Response):
    if not _WEB_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WEB_PASSWORD 未配置，拒绝访问。请在 .env 设置 WEB_USER/WEB_PASSWORD。",
        )
    ok_user = secrets.compare_digest(body.username, _WEB_USER)
    ok_pass = secrets.compare_digest(body.password, _WEB_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="认证失败")
    _set_auth_cookies(response, body.username)
    return {
        "authenticated": True,
        "username": body.username,
        "mode": _settings.mode.value,
        "expires_in": _SESSION_TTL_SECONDS,
    }


@app.post("/api/auth/logout")
async def api_auth_logout(response: Response):
    _clear_auth_cookies(response)
    return {"authenticated": False, "mode": _settings.mode.value}


@app.get("/api/auth/me")
async def api_auth_me(user: str = Depends(_check_auth)):
    return {"authenticated": True, "username": user, "mode": _settings.mode.value}


async def _effective_strategy_paused() -> tuple[bool, str]:
    try:
        store = await _get_store()
        raw = await store.get_runtime_setting("strategy.paused")
        if raw is not None:
            return _parse_bool_setting(raw, False), "runtime"
    except Exception as e:
        logger.warning("runtime strategy status unavailable, fallback to running: {}", e)
    return False, "default"


async def _strategy_pause_meta() -> dict[str, str]:
    try:
        store = await _get_store()
        settings = await store.runtime_settings()
        return {
            "reason_code": settings.get("strategy.pause.reason_code", ""),
            "reason": settings.get("strategy.pause.reason", ""),
            "source": settings.get("strategy.pause.source", ""),
            "at_ms": settings.get("strategy.pause.at_ms", ""),
        }
    except Exception as e:
        logger.warning("runtime strategy pause meta unavailable: {}", e)
        return {"reason_code": "", "reason": "", "source": "", "at_ms": ""}


async def _effective_symbol_enabled() -> dict[str, bool]:
    try:
        store = await _get_store()
        rows = await store.list_symbols()
        return {
            row["symbol"]: bool(row["enabled"]) and not bool(row["needs_review"])
            for row in rows
            if row["status"] == "active"
        }
    except Exception as e:
        logger.warning("runtime symbol settings unavailable, fallback to enabled: {}", e)
    return {symbol: True for symbol in _settings.symbols}


async def _symbol_rows() -> list[dict[str, Any]]:
    try:
        store = await _get_store()
        rows = await store.list_symbols()
        return [row for row in rows if row["status"] == "active"]
    except Exception as e:
        logger.warning("symbol registry unavailable, fallback to config symbols: {}", e)
        return [
            {
                "symbol": symbol,
                "enabled": True,
                "status": "active",
                "sync_status": "config_fallback",
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
            for symbol in _settings.symbols
        ]


async def _registered_symbols() -> list[str]:
    rows = await _symbol_rows()
    return [normalize_symbol(row["symbol"]) for row in rows]


def _apply_live_balance(summary: dict[str, Any], live_balance: dict[str, Any]) -> None:
    balance = dict(summary.get("balance") or {})
    total = float(live_balance["total_equity"])
    balance.update({
        "ts_ms": int(live_balance["ts_ms"]),
        "total_equity": total,
        "available_margin": float(live_balance["available_margin"]),
        **st.day_equity_change(
            _DB,
            current_equity=total,
            now_ms=int(live_balance["ts_ms"]),
        ),
        "equity_source": "exchange",
    })
    summary["balance"] = balance


def _apply_account_risk_metrics(
    summary: dict[str, Any],
    *,
    equity_peak: float = 0.0,
    risk_day_key: str = "",
    risk_day_equity_peak: float = 0.0,
    drawdown_bypass_day: str = "",
) -> None:
    """Attach explicitly named account, position and reserved-margin metrics."""
    balance = dict(summary.get("balance") or {})
    positions = list(summary.get("positions") or [])
    regular_orders = list(summary.get("open_orders") or [])
    total_equity = max(float(balance.get("total_equity") or 0.0), 0.0)
    available_margin = max(float(balance.get("available_margin") or 0.0), 0.0)
    unrealized_pnl = sum(float(row.get("unrealized_pnl") or 0.0) for row in positions)
    floating_loss = max(-unrealized_pnl, 0.0)
    position_initial_margin = sum(
        max(float(row.get("initial_margin") or 0.0), 0.0) for row in positions
    )
    unavailable_margin = max(total_equity - available_margin, 0.0)
    order_reserved_estimate = max(unavailable_margin - position_initial_margin, 0.0)
    drawdown_pct = max(float(balance.get("drawdown_pct") or 0.0), 0.0)
    if equity_peak <= 0 and total_equity > 0 and drawdown_pct < 100:
        equity_peak = total_equity / (1.0 - drawdown_pct / 100.0)
    today = time.strftime("%Y-%m-%d", time.localtime())
    if risk_day_key != today:
        risk_day_key = today
        risk_day_equity_peak = total_equity
    risk_day_equity_peak = max(float(risk_day_equity_peak or 0.0), total_equity)
    risk_day_drawdown_pct = (
        max(0.0, (risk_day_equity_peak - total_equity) / risk_day_equity_peak * 100.0)
        if risk_day_equity_peak > 0 else 0.0
    )
    drawdown_bypass_active = bool(
        drawdown_bypass_day and drawdown_bypass_day == today
    )

    balance.update({
        # Backward-compatible drawdown_pct remains the circuit-breaker metric.
        "account_drawdown_pct": drawdown_pct,
        "account_equity_peak": max(float(equity_peak or 0.0), 0.0),
        "position_unrealized_pnl": unrealized_pnl,
        "position_floating_loss": floating_loss,
        "position_floating_loss_pct_equity": (
            floating_loss / total_equity * 100.0 if total_equity > 0 else 0.0
        ),
        "position_initial_margin": position_initial_margin,
        "unavailable_margin": unavailable_margin,
        "open_order_reserved_margin_estimate": order_reserved_estimate,
        "regular_open_order_count": len(regular_orders),
        "external_open_order_count": sum(
            1 for row in regular_orders
            if str(row.get("origin") or "EXTERNAL").upper() == "EXTERNAL"
        ),
        "risk_period": "CALENDAR_DAY",
        "risk_day_key": risk_day_key,
        "risk_day_equity_peak": risk_day_equity_peak,
        "risk_day_drawdown_pct": risk_day_drawdown_pct,
        "drawdown_bypass_day": drawdown_bypass_day if drawdown_bypass_active else "",
        "drawdown_bypass_active": drawdown_bypass_active,
        "drawdown_breaker_basis": "DAILY_EQUITY_HIGH_WATER_MARK",
    })
    summary["balance"] = balance


def _attach_projection_metadata(
    positions: list[dict[str, Any]], orders: list[dict[str, Any]]
) -> None:
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for order in orders:
        by_symbol.setdefault(str(order.get("symbol") or ""), []).append(order)
    local = st.open_trade_metadata(_DB)
    for position in positions:
        symbol = str(position.get("symbol") or "")
        related = [
            row for row in by_symbol.get(symbol, [])
            if row.get("status") in ("placed", "new", "open", "working", "partial")
        ]
        sl_orders = sorted(
            (row for row in related if row.get("kind") == "SL"),
            key=lambda row: (float(row.get("trigger_price") or 0), str(row.get("id") or "")),
        )
        tp_orders = sorted(
            (row for row in related if row.get("kind") == "TP"),
            key=lambda row: (float(row.get("trigger_price") or 0), str(row.get("id") or "")),
        )
        qty = abs(float(position.get("contracts") or 0.0))
        tp_ordered_qty = sum(
            qty if row.get("close_position") else max(0.0, float(row.get("qty") or 0.0))
            for row in tp_orders
        )
        tp_covered_qty = min(tp_ordered_qty, qty) if qty > 0 else tp_ordered_qty
        tp_coverage_pct = min(tp_covered_qty / qty, 1.0) if qty > 0 else 0.0
        origins = {str(row.get("origin") or "EXTERNAL") for row in related}
        authority = (
            next(iter(origins)) if len(origins) == 1
            else "MIXED" if origins
            else "NONE"
        )
        mode = {
            "ENGINE": "ENGINE",
            "EXTERNAL": "OBSERVE",
            "MIXED": "MIXED",
        }.get(authority, "UNPROTECTED")
        qty_tol = max(qty * 1e-6, 1e-12)
        conflicts: list[str] = []
        if qty > 0 and tp_ordered_qty - qty > qty_tol:
            conflicts.append("TP_OVER_COVERED")
        if len(sl_orders) > 1:
            conflicts.append("MULTIPLE_SL")
        if not sl_orders:
            status = "MISSING_SL"
        elif not tp_orders:
            status = "MISSING_TP"
        elif conflicts:
            status = "CONFLICT"
        elif qty > 0 and qty - tp_ordered_qty > qty_tol:
            status = "PARTIAL_TP_COVERAGE"
        else:
            status = "COMPLETE"
        sl = sl_orders[0] if sl_orders else None
        tp = tp_orders[0] if tp_orders else None
        position["protection_orders"] = related
        position["protection"] = {
            "sl": sl, "tp": tp, "sl_active": sl is not None, "tp_active": tp is not None,
            "missing_sl": sl is None, "missing_tp": tp is None,
            "sl_orders": sl_orders,
            "tp_orders": tp_orders,
            "tp_ordered_qty": tp_ordered_qty,
            "tp_covered_qty": tp_covered_qty,
            "tp_coverage_pct": tp_coverage_pct,
            "runner_qty": max(0.0, qty - tp_covered_qty),
            "authority": authority,
            "mode": mode,
            "status": status,
            "conflicts": conflicts,
        }
        metadata = local.get(symbol)
        if metadata:
            position.update(metadata)
            if not position.get("leverage") and metadata.get("local_leverage"):
                position["leverage"] = metadata["local_leverage"]


async def _status_summary() -> dict[str, Any]:
    summary = st.status_summary(_DB)
    try:
        store = await _get_store()
        live = await store.live_account_state()
        quote_balance = next(
            (row for row in live["balances"] if row["asset"] == _settings.account.quote_asset),
            None,
        )
        if quote_balance:
            _apply_live_balance(summary, {
                "ts_ms": quote_balance["updated_at_ms"],
                "total_equity": quote_balance["wallet_balance"],
                "available_margin": quote_balance["available_balance"],
            })
            summary["balance"]["equity_source"] = "account_projection"
        summary["positions"] = live["positions"]
        summary["positions_source"] = "account_projection"
        summary["positions_error"] = ""
        summary["positions_synced_at_ms"] = max(
            [int(row.get("updated_at_ms") or 0) for row in live["positions"]] or [None]
        )
        live_orders = list(live["open_orders"])
        summary["open_orders"] = [
            row for row in live_orders if row.get("order_class") == "regular"
        ]
        summary["condition_orders"] = [
            row for row in live_orders if row.get("order_class") == "algo"
        ]
        summary["all_open_orders"] = live_orders
        _attach_projection_metadata(summary["positions"], live_orders)
        if hasattr(store, "runtime_settings"):
            runtime_settings = await store.runtime_settings()
        elif hasattr(store, "get_runtime_setting"):
            keys = (
                "risk.equity_peak",
                "risk.drawdown.day_key",
                "risk.drawdown.day_equity_peak",
                "risk.drawdown.bypass_day",
            )
            runtime_settings = {
                key: await store.get_runtime_setting(key) for key in keys
            }
        else:
            runtime_settings = {}
        raw_peak = runtime_settings.get("risk.equity_peak")
        try:
            equity_peak = float(raw_peak or 0.0)
        except (TypeError, ValueError):
            equity_peak = 0.0
        try:
            risk_day_equity_peak = float(
                runtime_settings.get("risk.drawdown.day_equity_peak") or 0.0
            )
        except (TypeError, ValueError):
            risk_day_equity_peak = 0.0
        _apply_account_risk_metrics(
            summary,
            equity_peak=equity_peak,
            risk_day_key=runtime_settings.get("risk.drawdown.day_key", ""),
            risk_day_equity_peak=risk_day_equity_peak,
            drawdown_bypass_day=runtime_settings.get("risk.drawdown.bypass_day", ""),
        )
        summary["open_orders_error"] = ""
        summary["condition_orders_error"] = ""
        summary["open_orders_synced_at_ms"] = max(
            [int(row.get("updated_at_ms") or 0) for row in live["open_orders"]] or [None]
        )
    except Exception as e:
        logger.warning("account projection unavailable, fallback to db snapshots: {}", e)
        if summary.get("balance"):
            summary["balance"]["equity_source"] = "db_snapshot"
            summary["balance"]["equity_error"] = str(e)
        summary["positions_source"] = "db_snapshot"
        summary["positions_error"] = str(e)
        summary["condition_orders"] = []
        summary["condition_orders_error"] = ""
        summary["positions_synced_at_ms"] = None
        summary["open_orders"] = []
        summary["open_orders_error"] = ""
        summary["open_orders_synced_at_ms"] = None
        _apply_account_risk_metrics(summary)
    # B5：补 symbol_enabled + 标注「持仓 + 禁用」的孤儿币种，方便前端定位。
    try:
        symbol_enabled = await _effective_symbol_enabled()
    except Exception as e:
        logger.warning("summary symbol_enabled unavailable: {}", e)
        symbol_enabled = {}
    summary["symbol_enabled"] = symbol_enabled
    disabled_with_position = sorted({
        pos["symbol"] for pos in summary.get("positions", [])
        if symbol_enabled.get(pos["symbol"]) is False
    })
    summary["disabled_with_position"] = disabled_with_position
    return summary


async def _stream_status() -> dict[str, Any]:
    store = await _get_store()
    runtime = await store.runtime_settings()
    event_stats = await store.exchange_event_stats()
    now = int(time.time() * 1000)
    drift_count = await store.recent_drift_count(now - 86400 * 1000)
    updated = int(event_stats["last_event_at_ms"] or runtime.get("stream.updated_at_ms") or 0)
    return {
        "enabled": _settings.user_stream.enabled,
        "status": runtime.get("stream.status", "STARTING"),
        "reason": runtime.get("stream.reason", ""),
        "session_id": runtime.get("stream.session_id", ""),
        "updated_at_ms": updated,
        "last_resync_at_ms": int(runtime.get("stream.last_resync_at_ms") or 0),
        "event_lag_ms": max(0, now - updated) if updated else None,
        "pending_events": event_stats["pending_events"],
        "failed_events": event_stats["failed_events"],
        "drift_count_24h": drift_count,
        "entry_ready": runtime.get("stream.status") == "LIVE",
    }


# ---------- REST：只读数据 ----------
@app.get("/api/summary")
async def api_summary(_: str = Depends(_check_auth)):
    summary = await _status_summary()
    summary["stream"] = await _stream_status()
    return summary


@app.get("/api/stream-status")
async def api_stream_status(_: str = Depends(_check_auth)):
    return await _stream_status()


@app.get("/api/positions")
async def api_positions(_: str = Depends(_check_auth)):
    try:
        return (await (await _get_store()).live_account_state())["positions"]
    except Exception as e:
        logger.warning("live positions endpoint fallback to db snapshot: {}", e)
        return st.latest_positions(_DB)


@app.get("/api/decisions")
async def api_decisions(
    symbol: list[str] = Query(default_factory=list),
    type: list[str] = Query(default_factory=list),
    start_ts_ms: int | None = Query(default=None),
    end_ts_ms: int | None = Query(default=None),
    hide_symbol_disabled: bool = Query(default=False),
    hide_no_significant_change: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: str = Depends(_check_auth),
):
    return st.search_decisions(
        _DB,
        st.DecisionFilters(
            symbols=symbol,
            types=type,
            start_ts_ms=start_ts_ms,
            end_ts_ms=end_ts_ms,
            hide_symbol_disabled=hide_symbol_disabled,
            hide_no_significant_change=hide_no_significant_change,
            limit=limit,
            offset=offset,
        ),
    )


@app.get("/api/decisions/{decision_id}")
async def api_decision_detail(decision_id: int, _: str = Depends(_check_auth)):
    row = st.decision_detail(_DB, decision_id)
    if row is None:
        raise HTTPException(status_code=404, detail="decision not found")
    return row


@app.get("/api/orders")
async def api_orders(limit: int = 100, _: str = Depends(_check_auth)):
    return st.recent_orders(_DB, min(limit, 500))


@app.get("/api/open-orders")
async def api_open_orders(_: str = Depends(_check_auth)):
    """Return the engine's canonical current-order projection."""
    snapshot = await (await _get_store()).live_account_state()
    orders = list(snapshot.get("open_orders") or [])
    return {
        "open_orders": [row for row in orders if row.get("order_class") == "regular"],
        "condition_orders": [row for row in orders if row.get("order_class") == "algo"],
        "error": "",
        "synced_at_ms": max(
            [int(row.get("updated_at_ms") or 0) for row in orders] or [None]
        ),
    }


@app.get("/api/trades")
async def api_trades(
    symbol: list[str] = Query(default_factory=list),
    direction: list[str] = Query(default_factory=list),
    status: list[str] = Query(default_factory=list),
    exit_reason: list[str] = Query(default_factory=list),
    source: list[str] = Query(default_factory=list),
    start_ts_ms: int | None = Query(default=None),
    end_ts_ms: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: str = Depends(_check_auth),
):
    return st.search_trades(
        _DB,
        st.TradeFilters(
            symbols=symbol,
            directions=direction,
            statuses=status,
            exit_reasons=exit_reason,
            sources=source,
            start_ts_ms=start_ts_ms,
            end_ts_ms=end_ts_ms,
            limit=limit,
            offset=offset,
        ),
    )


@app.get("/api/rejects")
async def api_rejects(limit: int = 100, _: str = Depends(_check_auth)):
    return st.recent_rejects(_DB, min(limit, 500))


@app.get("/api/pnl")
async def api_pnl(
    range: str | None = Query(default=None),
    start_ts_ms: int | None = Query(default=None),
    end_ts_ms: int | None = Query(default=None),
    _: str = Depends(_check_auth),
):
    start, end, resolved_range = st.resolve_time_bounds(
        range_key=range,
        start_ts_ms=start_ts_ms,
        end_ts_ms=end_ts_ms,
    )
    stats = st.pnl_stats(_DB, st.PnlFilters(start_ts_ms=start, end_ts_ms=end))
    stats["range"] = {
        "key": resolved_range,
        "start_ts_ms": start,
        "end_ts_ms": end,
    }
    try:
        live = await (await _get_store()).live_account_state()
        live_balance = next(
            row for row in live["balances"] if row["asset"] == _settings.account.quote_asset
        )
        stats.update(st.day_equity_change(
            _DB,
            current_equity=float(live_balance["wallet_balance"]),
            now_ms=int(live_balance["updated_at_ms"]),
        ))
        stats["equity_source"] = "account_projection"
    except Exception as e:
        logger.warning("live pnl equity unavailable, fallback to db snapshot: {}", e)
        stats["equity_source"] = "db_snapshot"
        stats["equity_error"] = str(e)
    try:
        stats["day_unrealized_pnl"] = sum(
            float(position.get("unrealized_pnl") or 0.0)
            for position in live.get("positions", [])
        )
        stats["unrealized_source"] = "account_projection"
    except Exception as e:
        logger.warning("live unrealized pnl unavailable, fallback to db snapshot: {}", e)
        positions = st.latest_positions(_DB)
        stats["day_unrealized_pnl"] = sum(
            float(position.get("unrealized_pnl") or 0.0)
            for position in positions
        )
        stats["unrealized_source"] = "db_snapshot"
    return stats


@app.get("/api/equity")
async def api_equity(
    range: str | None = Query(default=None),
    start_ts_ms: int | None = Query(default=None),
    end_ts_ms: int | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    _: str = Depends(_check_auth),
):
    start, end, _resolved_range = st.resolve_time_bounds(
        range_key=range,
        start_ts_ms=start_ts_ms,
        end_ts_ms=end_ts_ms,
    )
    return st.balance_history(
        _DB,
        min(limit, 2000),
        start_ts_ms=start,
        end_ts_ms=end,
    )


@app.get("/api/commands")
async def api_commands(limit: int = 50, _: str = Depends(_check_auth)):
    return st.recent_commands(_DB, min(limit, 200))


@app.get("/api/config")
async def api_config(_: str = Depends(_check_auth)):
    """暴露非敏感运行配置，供前端展示风控阈值等。"""
    s = _settings
    strategy_paused, strategy_status_source = await _effective_strategy_paused()
    strategy_pause = await _strategy_pause_meta()
    symbol_enabled = await _effective_symbol_enabled()
    symbols_state = await _symbol_rows()
    symbols = [row["symbol"] for row in symbols_state]
    store = await _get_store()
    effective_risk = decode_risk(
        await store.get_runtime_setting(RUNTIME_RISK_KEY), s.risk
    )
    risk_version = int(await store.get_runtime_setting(RUNTIME_RISK_VERSION_KEY) or 0)
    engine_defaults = engine_defaults_from_settings(s)
    effective_engine = decode_engine(
        await store.get_runtime_setting(RUNTIME_ENGINE_KEY), engine_defaults
    )
    engine_version = int(await store.get_runtime_setting(RUNTIME_ENGINE_VERSION_KEY) or 0)
    execution_defaults = execution_defaults_from_settings(s)
    effective_execution = decode_execution(
        await store.get_runtime_setting(RUNTIME_EXECUTION_KEY),
        execution_defaults,
        allowed_symbols=set(symbols),
    )
    execution_version = int(await store.get_runtime_setting(RUNTIME_EXECUTION_VERSION_KEY) or 0)
    return {
        "mode": s.mode.value,
        "db_path": s.storage.db_path,
        "symbols": symbols,
        "strategy_paused": strategy_paused,
        "strategy_status_source": strategy_status_source,
        "strategy_pause": strategy_pause,
        "symbol_enabled": symbol_enabled,
        "symbols_state": symbols_state,
        "cycle_interval": s.cycle.interval,
        "cycle_interval_seconds": effective_engine.cycle_interval_seconds,
        # 看板默认行情源（mainnet 真实价 / testnet 沙盒），供前端初始化源切换
        "market_source": _DEFAULT_SOURCE,
        "risk": risk_public(effective_risk),
        "risk_defaults": risk_public(s.risk),
        "risk_version": risk_version,
        "engine": engine_public(effective_engine),
        "engine_defaults": engine_public(engine_defaults),
        "engine_version": engine_version,
        "execution": execution_public(effective_execution),
        "execution_defaults": execution_public(execution_defaults),
        "execution_fixed": execution_fixed_public(s.execution),
        "execution_version": execution_version,
        "user_stream": await _stream_status(),
    }


@app.get("/api/symbols")
async def api_symbols(_: str = Depends(_check_auth)):
    return await _symbol_rows()


# ---------- REST：行情（K线 + 指标，只读交易所）----------
def _norm_source(source: str | None) -> str:
    """归一化行情源；非法值回落到默认源。"""
    s = (source or _DEFAULT_SOURCE).lower()
    return s if s in ("mainnet", "testnet") else _DEFAULT_SOURCE


@app.get("/api/klines/{symbol}")
async def api_klines(symbol: str, timeframe: str = "5m", limit: int = 200,
                     source: str | None = None, _: str = Depends(_check_auth)):
    """返回 K 线 + 最新指标快照，供前端 KLineCharts 渲染。

    source=mainnet|testnet：选择行情数据源。默认主网（真实价）。
    """
    symbol = normalize_symbol(symbol)
    if symbol not in await _registered_symbols():
        raise HTTPException(status_code=400, detail=f"symbol not registered: {symbol}")
    src = _norm_source(source)
    try:
        feed = await _feeds.get(src)
        klines = await feed.fetch_ohlcv(symbol, timeframe, min(limit, 1500))
    except Exception as e:
        logger.warning("klines fetch failed {} [{}]: {}", symbol, src, e)
        raise HTTPException(status_code=502, detail=f"exchange error: {e}")
    indicators = compute_snapshot(klines) if len(klines) >= 30 else {}
    # 当前持仓的开仓价/方向，供前端在图上标注
    pos = next((p for p in st.latest_positions(_DB) if p["symbol"] == symbol), None)
    return {"symbol": symbol, "timeframe": timeframe, "source": src,
            "klines": klines, "indicators": indicators, "position": pos}


@app.get("/api/ticker/{symbol}")
async def api_ticker(symbol: str, source: str | None = None,
                     _: str = Depends(_check_auth)):
    """轻量最新价/标记价。WS 不可用时的回退；正常实时价走 /ws/market。"""
    symbol = normalize_symbol(symbol)
    if symbol not in await _registered_symbols():
        raise HTTPException(status_code=400, detail=f"symbol not registered: {symbol}")
    src = _norm_source(source)
    try:
        feed = await _feeds.get(src)
        t = await feed.fetch_ticker(symbol)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"exchange error: {e}")
    return {
        "symbol": symbol,
        "source": src,
        "last": t.get("last"),
        "mark": t.get("mark") or (t.get("info") or {}).get("markPrice"),
        "change_24h_pct": t.get("percentage"),
        "ts": t.get("timestamp"),
    }


# ---------- 操作类：写命令队列（不直接碰交易所）----------
_ALLOWED_COMMANDS = {
    "KILL_SWITCH",
    "PAUSE",
    "RESUME",
    "RESUME_ALL_SYMBOLS",
    "SET_SYMBOL_ENABLED",
    "ADD_SYMBOL",
    "REVIEW_SYMBOL",
    "REPAIR_SL_TP",
    "PROTECT_POSITION",
    "CLOSE_POSITION",
    "CANCEL_OPEN_ORDER",
    "CANCEL_CONDITION_ORDER",
    "CANCEL_ALL_OPEN_ORDERS",
    "CANCEL_AND_FLATTEN",
    "STOP_ENGINE",
    "SWITCH_LLM_PROFILE",
    "RELOAD_LLM_PROMPT",
    "UPDATE_RISK_SETTINGS",
    "UPDATE_ENGINE_SETTINGS",
    "UPDATE_EXECUTION_SETTINGS",
}

_MAINNET_HIGH_RISK_COMMANDS = {
    "KILL_SWITCH", "RESUME", "RESUME_ALL_SYMBOLS", "SET_SYMBOL_ENABLED",
    "CLOSE_POSITION", "CANCEL_AND_FLATTEN", "STOP_ENGINE",
    "UPDATE_RISK_SETTINGS", "UPDATE_ENGINE_SETTINGS", "UPDATE_EXECUTION_SETTINGS",
    "RELOAD_LLM_PROMPT",
}


def _require_environment(expected: str | None) -> None:
    if expected and expected.lower() != _settings.mode.value:
        raise HTTPException(status_code=409, detail="target environment mismatch")


def _check_mainnet_origin(request: Request) -> None:
    if not _settings.is_mainnet:
        return
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise HTTPException(status_code=403, detail="cross-site mainnet mutation rejected")
    origin = request.headers.get("origin", "")
    host = request.headers.get("host", "")
    if origin and host and not origin.endswith(f"://{host}"):
        raise HTTPException(status_code=403, detail="mainnet origin mismatch")


def _payload_hash(action: str, payload: str) -> str:
    canonical = payload
    try:
        canonical = json.dumps(json.loads(payload), sort_keys=True, separators=(",", ":"))
    except Exception:
        pass
    return hashlib.sha256(f"{_settings.mode.value}:{action}:{canonical}".encode()).hexdigest()


def _canonical_reconcile_payload(run_id: int, preview_hash: str, days: int) -> str:
    return json.dumps(
        {"run_id": int(run_id), "preview_hash": preview_hash, "days": int(days)},
        sort_keys=True,
        separators=(",", ":"),
    )


def _consume_confirmation(token: str, action: str, payload: str) -> None:
    if not _settings.is_mainnet:
        return
    item = _confirmations.pop(token, None)
    if not item or item["expires_at"] < time.time():
        raise HTTPException(status_code=403, detail="mainnet confirmation missing or expired")
    if item["hash"] != _payload_hash(action, payload):
        raise HTTPException(status_code=403, detail="mainnet confirmation payload mismatch")


class _ConfirmationRequest(BaseModel):
    action: str = Field(min_length=1, max_length=64)
    payload: str = Field(default="", max_length=20000)
    confirmation: str = Field(min_length=1, max_length=32)


class _RiskUpdateRequest(BaseModel):
    expected_version: int = Field(ge=0)
    values: dict[str, Any]
    confirmation_token: str = ""


class _EngineUpdateRequest(BaseModel):
    expected_version: int = Field(ge=0)
    values: dict[str, Any]
    confirmation_token: str = ""


class _ExecutionUpdateRequest(BaseModel):
    expected_version: int = Field(ge=0)
    values: dict[str, Any]
    confirmation_token: str = ""


class _TradeReconcilePreviewRequest(BaseModel):
    days: int = Field(default=30, ge=1, le=90)


class _TradeReconcileApplyRequest(BaseModel):
    run_id: int = Field(gt=0)
    preview_hash: str = Field(min_length=64, max_length=64)
    days: int = Field(default=30, ge=1, le=90)
    confirmation_token: str = ""


@app.post("/api/confirmations")
async def api_confirmation(
    body: _ConfirmationRequest,
    request: Request,
    expected_environment: str | None = Header(default=None, alias="X-Trade-Environment"),
    _: str = Depends(_check_auth),
):
    _check_mainnet_origin(request)
    _require_environment(expected_environment)
    if not _settings.is_mainnet:
        return {"token": "", "expires_in": 0}
    if body.confirmation != "MAINNET":
        raise HTTPException(status_code=403, detail="type MAINNET to confirm")
    token = secrets.token_urlsafe(32)
    _confirmations[token] = {
        "hash": _payload_hash(body.action.upper(), body.payload),
        "expires_at": time.time() + _MAINNET_CONFIRM_TTL_SECONDS,
    }
    return {"token": token, "expires_in": _MAINNET_CONFIRM_TTL_SECONDS}


async def _run_trade_reconcile_preview(days: int) -> dict[str, Any]:
    client = ExchangeClient(_settings, _creds)
    try:
        reconciler = BinanceTradeReconciler(await _get_store(), client, _DB)
        result = await reconciler.preview(days=days)
        result.pop("_resolved_fills", None)
        return result
    finally:
        await client.close()


@app.post("/api/trades/reconcile/preview")
async def api_trade_reconcile_preview(
    body: _TradeReconcilePreviewRequest,
    _: str = Depends(_check_auth),
):
    if _reconcile_lock.locked():
        raise HTTPException(status_code=409, detail="已有 Binance 成交核对任务正在运行")
    async with _reconcile_lock:
        try:
            return await _run_trade_reconcile_preview(body.days)
        except ReconcileError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Binance trade reconcile preview failed")
            raise HTTPException(status_code=502, detail=f"核对失败：{exc}") from exc


@app.post("/api/trades/reconcile/apply")
async def api_trade_reconcile_apply(
    body: _TradeReconcileApplyRequest,
    request: Request,
    expected_environment: str | None = Header(default=None, alias="X-Trade-Environment"),
    _: str = Depends(_check_auth),
):
    _require_environment(expected_environment)
    _check_mainnet_origin(request)
    payload = _canonical_reconcile_payload(body.run_id, body.preview_hash, body.days)
    _consume_confirmation(
        body.confirmation_token, "RECONCILE_BINANCE_TRADES", payload
    )
    if _reconcile_lock.locked():
        raise HTTPException(status_code=409, detail="已有 Binance 成交核对任务正在运行")
    async with _reconcile_lock:
        client = ExchangeClient(_settings, _creds)
        try:
            reconciler = BinanceTradeReconciler(await _get_store(), client, _DB)
            return await reconciler.apply(
                run_id=body.run_id,
                preview_hash=body.preview_hash,
                days=body.days,
            )
        except ReconcileError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Binance trade reconcile apply failed")
            raise HTTPException(status_code=502, detail=f"修复失败：{exc}") from exc
        finally:
            await client.close()


@app.post("/api/command/{name}")
async def api_command(
    name: str,
    request: Request,
    arg: str = "",
    confirmation_token: str = "",
    expected_environment: str | None = Header(default=None, alias="X-Trade-Environment"),
    user: str = Depends(_check_auth),
):
    """下发控制命令。交易进程快速消费执行。"""
    name = name.upper()
    if name not in _ALLOWED_COMMANDS:
        raise HTTPException(status_code=400, detail=f"unknown command: {name}")
    _require_environment(expected_environment)
    _check_mainnet_origin(request)
    if name in _MAINNET_HIGH_RISK_COMMANDS:
        _consume_confirmation(confirmation_token, name, arg)
    store = await _get_store()
    cmd_id = await store.enqueue_command(name, arg=arg, source=f"web:{user}")
    logger.warning("web command queued: {} arg={} by={} id={}", name, arg, user, cmd_id)
    return {"queued": True, "id": cmd_id, "command": name,
            "note": "交易进程将尽快消费执行"}


@app.get("/api/risk-settings")
async def api_risk_settings(_: str = Depends(_check_auth)):
    store = await _get_store()
    effective = decode_risk(await store.get_runtime_setting(RUNTIME_RISK_KEY), _settings.risk)
    version = int(await store.get_runtime_setting(RUNTIME_RISK_VERSION_KEY) or 0)
    return {
        "mode": _settings.mode.value,
        "version": version,
        "defaults": risk_public(_settings.risk),
        "effective": risk_public(effective),
    }


@app.post("/api/risk-settings/preview")
async def api_risk_settings_preview(body: _RiskUpdateRequest, _: str = Depends(_check_auth)):
    store = await _get_store()
    current = decode_risk(await store.get_runtime_setting(RUNTIME_RISK_KEY), _settings.risk)
    current_version = int(await store.get_runtime_setting(RUNTIME_RISK_VERSION_KEY) or 0)
    if body.expected_version != current_version:
        raise HTTPException(status_code=409, detail=f"version conflict: current={current_version}")
    try:
        updated = validate_risk_payload(body.values, current)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    before = risk_public(current)
    after = risk_public(updated)
    balance = st.latest_balance(_DB) or {}
    equity = float(balance.get("total_equity") or 0.0)
    day_pnl = float(balance.get("day_realized_pnl") or 0.0)
    drawdown = float(balance.get("drawdown_pct") or 0.0)
    daily_limit = equity * updated.daily_max_loss_pct / 100.0
    return {
        "mode": _settings.mode.value,
        "version": current_version,
        "changes": {
            key: {"before": before[key], "after": after[key]}
            for key in after if before[key] != after[key]
        },
        "effective": after,
        "impact": {
            "equity": equity,
            "day_realized_pnl": day_pnl,
            "drawdown_pct": drawdown,
            "would_trigger_daily_loss": daily_limit > 0 and day_pnl <= -daily_limit,
            "would_trigger_drawdown": drawdown >= updated.max_drawdown_pct,
        },
    }


@app.post("/api/risk-settings/apply")
async def api_risk_settings_apply(
    body: _RiskUpdateRequest,
    request: Request,
    expected_environment: str | None = Header(default=None, alias="X-Trade-Environment"),
    user: str = Depends(_check_auth),
):
    _require_environment(expected_environment)
    _check_mainnet_origin(request)
    payload = json.dumps(
        {"expected_version": body.expected_version, **body.values},
        sort_keys=True, separators=(",", ":"),
    )
    _consume_confirmation(body.confirmation_token, "UPDATE_RISK_SETTINGS", payload)
    store = await _get_store()
    cmd_id = await store.enqueue_command(
        "UPDATE_RISK_SETTINGS", arg=payload, source=f"web:{user}"
    )
    return {"queued": True, "id": cmd_id, "command": "UPDATE_RISK_SETTINGS"}


@app.get("/api/engine-settings")
async def api_engine_settings(_: str = Depends(_check_auth)):
    store = await _get_store()
    defaults = engine_defaults_from_settings(_settings)
    effective = decode_engine(await store.get_runtime_setting(RUNTIME_ENGINE_KEY), defaults)
    version = int(await store.get_runtime_setting(RUNTIME_ENGINE_VERSION_KEY) or 0)
    return {
        "mode": _settings.mode.value,
        "version": version,
        "defaults": engine_public(defaults),
        "effective": engine_public(effective),
    }


@app.post("/api/engine-settings/preview")
async def api_engine_settings_preview(
    body: _EngineUpdateRequest,
    _: str = Depends(_check_auth),
):
    store = await _get_store()
    defaults = engine_defaults_from_settings(_settings)
    current = decode_engine(await store.get_runtime_setting(RUNTIME_ENGINE_KEY), defaults)
    current_version = int(await store.get_runtime_setting(RUNTIME_ENGINE_VERSION_KEY) or 0)
    if body.expected_version != current_version:
        raise HTTPException(status_code=409, detail=f"version conflict: current={current_version}")
    try:
        updated = validate_engine_payload(body.values, current)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    before = engine_public(current)
    after = engine_public(updated)
    return {
        "mode": _settings.mode.value,
        "version": current_version,
        "changes": {
            key: {"before": before[key], "after": after[key]}
            for key in after if before[key] != after[key]
        },
        "effective": after,
        "impact": {
            "cycle_interval_seconds": after["cycle_interval_seconds"],
            "shortest_review_seconds": min(
                after["review_flat_seconds"],
                after["review_position_seconds"],
                after["review_near_exit_seconds"],
                after["review_high_vol_seconds"],
            ),
            "llm_calls_overlap": False,
        },
    }


@app.post("/api/engine-settings/apply")
async def api_engine_settings_apply(
    body: _EngineUpdateRequest,
    request: Request,
    expected_environment: str | None = Header(default=None, alias="X-Trade-Environment"),
    user: str = Depends(_check_auth),
):
    _require_environment(expected_environment)
    _check_mainnet_origin(request)
    payload = json.dumps(
        {"expected_version": body.expected_version, **body.values},
        sort_keys=True, separators=(",", ":"),
    )
    _consume_confirmation(body.confirmation_token, "UPDATE_ENGINE_SETTINGS", payload)
    store = await _get_store()
    cmd_id = await store.enqueue_command(
        "UPDATE_ENGINE_SETTINGS", arg=payload, source=f"web:{user}"
    )
    return {"queued": True, "id": cmd_id, "command": "UPDATE_ENGINE_SETTINGS"}


async def _execution_allowed_symbols() -> set[str]:
    rows = await _symbol_rows()
    symbols = {str(row["symbol"]).upper() for row in rows if row.get("symbol")}
    return symbols or set(_settings.symbols)


def _execution_impact(after: dict[str, Any]) -> dict[str, Any]:
    attempts = int(after["maker_max_requotes"]) + 1
    timeout = float(after["maker_timeout_seconds"])
    return {
        "maker_attempts": attempts,
        "worst_maker_wait_seconds": round(attempts * timeout, 3),
        "fallback_market": after["maker_unfilled_action"] == "FALLBACK_MARKET",
        "entry_mode": after["entry_mode"],
        "market_slippage_bps": after["market_slippage_bps"],
    }


@app.get("/api/execution-settings")
async def api_execution_settings(_: str = Depends(_check_auth)):
    store = await _get_store()
    defaults = execution_defaults_from_settings(_settings)
    effective = decode_execution(
        await store.get_runtime_setting(RUNTIME_EXECUTION_KEY),
        defaults,
        allowed_symbols=await _execution_allowed_symbols(),
    )
    version = int(await store.get_runtime_setting(RUNTIME_EXECUTION_VERSION_KEY) or 0)
    return {
        "mode": _settings.mode.value,
        "version": version,
        "defaults": execution_public(defaults),
        "effective": execution_public(effective),
        "fixed": execution_fixed_public(_settings.execution),
    }


@app.post("/api/execution-settings/preview")
async def api_execution_settings_preview(
    body: _ExecutionUpdateRequest,
    _: str = Depends(_check_auth),
):
    store = await _get_store()
    defaults = execution_defaults_from_settings(_settings)
    allowed_symbols = await _execution_allowed_symbols()
    current = decode_execution(
        await store.get_runtime_setting(RUNTIME_EXECUTION_KEY),
        defaults,
        allowed_symbols=allowed_symbols,
    )
    current_version = int(await store.get_runtime_setting(RUNTIME_EXECUTION_VERSION_KEY) or 0)
    if body.expected_version != current_version:
        raise HTTPException(status_code=409, detail=f"version conflict: current={current_version}")
    try:
        updated = validate_execution_payload(
            body.values,
            current,
            allowed_symbols=allowed_symbols,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    before = execution_public(current)
    after = execution_public(updated)
    return {
        "mode": _settings.mode.value,
        "version": current_version,
        "changes": {
            key: {"before": before[key], "after": after[key]}
            for key in after if before[key] != after[key]
        },
        "effective": after,
        "fixed": execution_fixed_public(_settings.execution),
        "impact": _execution_impact(after),
    }


@app.post("/api/execution-settings/apply")
async def api_execution_settings_apply(
    body: _ExecutionUpdateRequest,
    request: Request,
    expected_environment: str | None = Header(default=None, alias="X-Trade-Environment"),
    user: str = Depends(_check_auth),
):
    _require_environment(expected_environment)
    _check_mainnet_origin(request)
    payload = json.dumps(
        {"expected_version": body.expected_version, **body.values},
        sort_keys=True, separators=(",", ":"),
    )
    _consume_confirmation(body.confirmation_token, "UPDATE_EXECUTION_SETTINGS", payload)
    store = await _get_store()
    cmd_id = await store.enqueue_command(
        "UPDATE_EXECUTION_SETTINGS", arg=payload, source=f"web:{user}"
    )
    return {"queued": True, "id": cmd_id, "command": "UPDATE_EXECUTION_SETTINGS"}


# ---------- LLM Prompt 管理 ----------
from src.llm.prompt import (  # noqa: E402
    DEFAULT_SYSTEM_PROMPT_TEMPLATE,
    DEFAULT_USER_PROMPT_TEMPLATE,
    RENDER_MODE_FULL_TEMPLATE,
    RENDER_MODE_LEGACY_APPEND,
    build_system_prompt,
    render_prompts,
)
from src.llm.schema import IndicatorSnapshot, MarketContext, PositionSnapshot  # noqa: E402


class _LLMPromptApplyRequest(BaseModel):
    name: str = Field(default="", max_length=80)
    content: str = Field(default="", max_length=20000)
    render_mode: str = Field(default=RENDER_MODE_LEGACY_APPEND, max_length=24)
    system_prompt_template: str = Field(default="", max_length=60000)
    user_prompt_template: str = Field(default="", max_length=60000)
    notes: str = Field(default="", max_length=20000)
    confirmation_token: str = ""


class _LLMPromptPreviewRequest(BaseModel):
    content: str = Field(default="", max_length=20000)
    render_mode: str = Field(default=RENDER_MODE_LEGACY_APPEND, max_length=24)
    system_prompt_template: str = Field(default="", max_length=60000)
    user_prompt_template: str = Field(default="", max_length=60000)
    symbol: str = Field(default="BTCUSDT", max_length=20)


class _LLMPromptValidateRequest(_LLMPromptPreviewRequest):
    symbols: list[str] = Field(default_factory=list, max_length=8)


class _LLMPromptActivateRequest(BaseModel):
    confirmation_token: str = ""


def _prompt_engine_status(rt: dict[str, str]) -> dict[str, Any]:
    try:
        version = int(rt.get("llm.prompt_version", "0") or "0")
    except (TypeError, ValueError):
        version = 0
    return {
        "version": version,
        "name": rt.get("llm.prompt_name", ""),
        "source": rt.get("llm.prompt_source", ""),
    }


def _default_full_prompt_version() -> dict[str, Any]:
    return {
        "id": None,
        "version": 0,
        "name": "代码默认完整 Prompt",
        "content": "",
        "render_mode": RENDER_MODE_FULL_TEMPLATE,
        "system_prompt_template": DEFAULT_SYSTEM_PROMPT_TEMPLATE,
        "user_prompt_template": DEFAULT_USER_PROMPT_TEMPLATE,
        "template_schema_version": 1,
        "notes": "未保存的默认模板；保存后会成为 v1。",
        "is_active": False,
        "source": "code",
        "created_at": "",
        "updated_at": "",
    }


def _sample_market_context(symbol: str = "BTCUSDT") -> MarketContext:
    sym = normalize_symbol(symbol)
    klines = []
    base = 65000.0 if sym == "BTCUSDT" else 3000.0
    ts0 = int(time.time() * 1000) - 25 * 300_000
    for i in range(25):
        close = base + i * 3.0
        klines.append([ts0 + i * 300_000, close - 5, close + 10, close - 12, close, 100 + i])
    return MarketContext(
        symbol=sym,
        timestamp=klines[-1][0],
        last_price=klines[-1][4],
        mark_price=klines[-1][4],
        funding_rate=0.0,
        change_24h_pct=0.0,
        recent_klines=klines,
        indicators=IndicatorSnapshot(
            ema_fast=klines[-1][4] * 0.999,
            ema_slow=klines[-1][4] * 0.998,
            rsi=55.0,
            macd=0.1,
            macd_signal=0.05,
            atr=10.0,
            boll_upper=klines[-1][4] * 1.01,
            boll_lower=klines[-1][4] * 0.99,
        ),
        position=PositionSnapshot(),
        available_margin=1000.0,
        max_leverage_allowed=5,
        account_equity=1000.0,
        max_order_margin_abs=200.0,
        max_order_margin_pct=0.2,
        max_loss_per_trade_abs=60.0,
    )


async def _prompt_context(store: Store, symbol: str) -> tuple[MarketContext, str]:
    raw = await store.latest_decision_context_json(symbol)
    if raw:
        try:
            return MarketContext.model_validate_json(raw), "latest_decision"
        except Exception as e:  # noqa: BLE001
            logger.warning("invalid latest context json for {}: {}", symbol, e)
    return _sample_market_context(symbol), "sample"


def _prompt_version_from_request(body: _LLMPromptPreviewRequest) -> dict[str, Any]:
    mode = (body.render_mode or RENDER_MODE_LEGACY_APPEND).strip()
    if mode == RENDER_MODE_FULL_TEMPLATE:
        return {
            "render_mode": RENDER_MODE_FULL_TEMPLATE,
            "system_prompt_template": body.system_prompt_template or DEFAULT_SYSTEM_PROMPT_TEMPLATE,
            "user_prompt_template": body.user_prompt_template or DEFAULT_USER_PROMPT_TEMPLATE,
            "content": body.content or "",
        }
    return {"render_mode": RENDER_MODE_LEGACY_APPEND, "content": body.content or ""}


@app.get("/api/llm/prompt")
async def api_llm_prompt(_: str = Depends(_check_auth)):
    store = await _get_store()
    active = await store.get_active_llm_prompt_version()
    versions = await store.list_llm_prompt_versions()
    rt = await store.runtime_settings()
    effective_active = active or _default_full_prompt_version()
    return {
        "mode": _settings.mode.value,
        "active": effective_active,
        "versions": versions,
        "engine": _prompt_engine_status(rt),
        "default_system_prompt_template": DEFAULT_SYSTEM_PROMPT_TEMPLATE,
        "default_user_prompt_template": DEFAULT_USER_PROMPT_TEMPLATE,
        "effective_system_prompt": (
            build_system_prompt((active or {}).get("content", ""))
            if (active or {}).get("render_mode") != RENDER_MODE_FULL_TEMPLATE
            else (active or {}).get("system_prompt_template", DEFAULT_SYSTEM_PROMPT_TEMPLATE)
        ),
    }


@app.post("/api/llm/prompt/preview")
async def api_llm_prompt_preview(body: _LLMPromptPreviewRequest, _: str = Depends(_check_auth)):
    store = await _get_store()
    ctx, context_source = await _prompt_context(store, body.symbol)
    system_prompt, user_prompt, warnings = render_prompts(
        ctx=ctx,
        prompt_version=_prompt_version_from_request(body),
        kline_interval=_settings.llm.kline_interval,
        prompt_kline_count=_settings.llm.prompt_kline_count,
        micro_kline_count=_settings.llm.micro_kline_lookback,
    )
    return {
        "effective_system_prompt": system_prompt,
        "effective_user_prompt": user_prompt,
        "warnings": warnings,
        "context_source": context_source,
        "symbol": ctx.symbol,
    }


@app.post("/api/llm/prompt/validate")
async def api_llm_prompt_validate(
    body: _LLMPromptValidateRequest,
    request: Request,
    expected_environment: str | None = Header(default=None, alias="X-Trade-Environment"),
    _: str = Depends(_check_auth),
):
    _require_environment(expected_environment)
    _check_mainnet_origin(request)
    store = await _get_store()
    prof = await store.get_active_llm_profile()
    if prof is None:
        raise HTTPException(status_code=409, detail="no active llm profile")
    api_key = await store.get_llm_profile_secret(prof["name"])
    if not api_key:
        raise HTTPException(status_code=409, detail=f"active profile {prof['name']} has no api_key")
    from src.llm.providers import build_provider  # noqa: E402

    provider = build_provider(
        prof["provider"],
        model=prof["model"],
        base_url=(prof.get("base_url") or None),
        api_key=api_key,
        timeout=min(float(prof.get("timeout") or 60), 60.0),
    )
    symbols = [normalize_symbol(s) for s in (body.symbols or [body.symbol]) if s]
    if not symbols:
        raise HTTPException(status_code=400, detail="symbols required")
    allowed = set(await _registered_symbols())
    results: list[dict[str, Any]] = []
    prompt_version = _prompt_version_from_request(body)
    max_tokens = min(int(prof.get("max_tokens") or 1024), 4096)
    try:
        for symbol in symbols:
            if symbol not in allowed:
                results.append({"symbol": symbol, "ok": False, "error": "symbol not registered"})
                continue
            ctx, context_source = await _prompt_context(store, symbol)
            system_prompt, user_prompt, warnings = render_prompts(
                ctx=ctx,
                prompt_version=prompt_version,
                kline_interval=_settings.llm.kline_interval,
                prompt_kline_count=_settings.llm.prompt_kline_count,
                micro_kline_count=_settings.llm.micro_kline_lookback,
            )
            t0 = time.monotonic()
            try:
                resp = await provider.create(
                    system=system_prompt,
                    user_prompt=user_prompt,
                    max_tokens=max_tokens,
                )
                decision = provider.parse(resp, symbol)
                latency = int((time.monotonic() - t0) * 1000)
                if decision is None:
                    results.append({
                        "symbol": symbol,
                        "ok": False,
                        "context_source": context_source,
                        "latency_ms": latency,
                        "warnings": warnings,
                        "error": "LLM response did not match TradeDecision schema/tool result",
                    })
                    continue
                if decision.symbol != symbol:
                    results.append({
                        "symbol": symbol,
                        "ok": False,
                        "context_source": context_source,
                        "latency_ms": latency,
                        "warnings": warnings,
                        "error": f"LLM response symbol mismatch: {decision.symbol}",
                    })
                    continue
                results.append({
                    "symbol": symbol,
                    "ok": True,
                    "context_source": context_source,
                    "latency_ms": latency,
                    "warnings": warnings,
                    "decision": decision.model_dump(mode="json"),
                })
            except Exception as e:  # noqa: BLE001
                results.append({
                    "symbol": symbol,
                    "ok": False,
                    "context_source": context_source,
                    "warnings": warnings,
                    "error": f"{type(e).__name__}: {e}",
                })
    finally:
        try:
            await provider.close()
        except Exception:  # noqa: BLE001
            pass
    return {"profile": prof["name"], "model": prof["model"], "results": results}


@app.post("/api/llm/prompt/{prompt_id}/activate")
async def api_llm_prompt_activate(
    prompt_id: int,
    body: _LLMPromptActivateRequest,
    request: Request,
    expected_environment: str | None = Header(default=None, alias="X-Trade-Environment"),
    user: str = Depends(_check_auth),
):
    _require_environment(expected_environment)
    _check_mainnet_origin(request)
    store = await _get_store()
    existing = await store.get_llm_prompt_version(prompt_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="llm prompt version not found")
    payload = json.dumps(
        {"id": int(existing["id"]), "version": int(existing["version"])},
        sort_keys=True, separators=(",", ":"),
    )
    _consume_confirmation(body.confirmation_token, "ACTIVATE_LLM_PROMPT", payload)
    version = await store.activate_llm_prompt_version(prompt_id)
    cmd_id = await store.enqueue_command(
        "RELOAD_LLM_PROMPT", arg=str(version["id"]), source=f"web:{user}"
    )
    return {
        "queued": True,
        "id": cmd_id,
        "command": "RELOAD_LLM_PROMPT",
        "active": version,
        "note": "engine 将在当前 LLM 调用结束后热加载所选 Prompt 版本",
    }


@app.post("/api/llm/prompt/apply")
async def api_llm_prompt_apply(
    body: _LLMPromptApplyRequest,
    request: Request,
    expected_environment: str | None = Header(default=None, alias="X-Trade-Environment"),
    user: str = Depends(_check_auth),
):
    _require_environment(expected_environment)
    _check_mainnet_origin(request)
    payload = json.dumps(
        {
            "name": body.name,
            "content": body.content,
            "render_mode": body.render_mode,
            "system_prompt_template": body.system_prompt_template,
            "user_prompt_template": body.user_prompt_template,
            "notes": body.notes,
        },
        sort_keys=True, separators=(",", ":"),
    )
    _consume_confirmation(body.confirmation_token, "UPDATE_LLM_PROMPT", payload)
    store = await _get_store()
    version = await store.create_llm_prompt_version(
        name=body.name,
        content=body.content,
        render_mode=body.render_mode,
        system_prompt_template=body.system_prompt_template,
        user_prompt_template=body.user_prompt_template,
        notes=body.notes,
        source=f"web:{user}",
        activate=True,
    )
    cmd_id = await store.enqueue_command(
        "RELOAD_LLM_PROMPT", arg=str(version["id"]), source=f"web:{user}"
    )
    return {
        "queued": True,
        "id": cmd_id,
        "command": "RELOAD_LLM_PROMPT",
        "active": version,
        "note": "engine 将在当前 LLM 调用结束后热加载新 Prompt",
    }


# ---------- LLM profile 管理 ----------
# 全部写 llm_profiles 表；激活通过现有命令队列触发 engine 热替换。
# 设计要点：
# - list / get / activate / test 永远不返回 key 明文（只回 key_present + 末4位 mask）。
# - 创建/更新时只接受 api_key 明文（POST/PUT body），明文直接入库（单租户自托管）。
# - fallback 链：active 主源 + fallback_enabled 备源，按 priority 升序串联。

from src.llm.providers import build_provider  # noqa: E402


class _LLMProfileUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    provider: str = Field(default="anthropic", pattern="^(anthropic|openai_compatible)$")
    model: str = Field(min_length=1, max_length=128)
    base_url: str | None = None
    timeout: float = Field(default=60.0, gt=0)
    max_tokens: int = Field(default=1024, gt=0, le=512000)
    max_retries: int = Field(default=2, ge=0, le=5)
    priority: int = Field(default=100, ge=0, le=10000)
    fallback_enabled: bool = False
    # 留空=不更新（PUT 时）；POST 时必填
    api_key: str = Field(default="", max_length=8192)


@app.get("/api/llm/status")
async def api_llm_status(_: str = Depends(_check_auth)):
    """前端切换状态轮询用（active / engine / fallback 链）。"""
    store = await _get_store()
    active = await store.get_active_llm_profile()
    chain = await store.get_enabled_llm_profiles()
    rt = await store.runtime_settings()
    # engine 热替换后会写 llm.active_version / llm.active_name / llm.active_source / llm.chain
    return {
        "active": active,
        "switching_supported": True,
        "chain": [
            {"name": p["name"], "provider": p["provider"], "priority": p["priority"],
             "is_active": p["is_active"], "fallback_enabled": p["fallback_enabled"]}
            for p in chain
        ],
        "engine": {
            "active_name": rt.get("llm.active_name", ""),
            "active_version": int(rt.get("llm.active_version", "0") or "0"),
            "active_source": rt.get("llm.active_source", ""),
            "chain": rt.get("llm.chain", ""),
        },
    }


@app.get("/api/llm/profiles")
async def api_llm_profiles(_: str = Depends(_check_auth)):
    store = await _get_store()
    return {"items": await store.list_llm_profiles()}


@app.post("/api/llm/profiles", status_code=201)
async def api_llm_profiles_create(
    payload: _LLMProfileUpsert, _: str = Depends(_check_auth)
):
    if not payload.api_key.strip():
        raise HTTPException(
            status_code=400, detail="api_key is required when creating a profile"
        )
    store = await _get_store()
    existing = await store.get_llm_profile(payload.name)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"profile exists: {payload.name}")
    prof = await store.upsert_llm_profile(
        name=payload.name,
        provider=payload.provider,
        model=payload.model,
        base_url=(payload.base_url or None),
        timeout=payload.timeout,
        max_tokens=payload.max_tokens,
        max_retries=payload.max_retries,
        api_key=payload.api_key,
        priority=payload.priority,
        fallback_enabled=payload.fallback_enabled,
    )
    return prof


@app.put("/api/llm/profiles/{name}")
async def api_llm_profiles_update(
    name: str, payload: _LLMProfileUpsert, _: str = Depends(_check_auth)
):
    store = await _get_store()
    existing = await store.get_llm_profile(name)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"profile not found: {name}")
    # api_key 留空 = 保留旧 key；非空 = 覆盖（repo 内部按是否非空决定）。
    prof = await store.upsert_llm_profile(
        name=name,
        provider=payload.provider,
        model=payload.model,
        base_url=(payload.base_url or None),
        timeout=payload.timeout,
        max_tokens=payload.max_tokens,
        max_retries=payload.max_retries,
        api_key=payload.api_key,
        priority=payload.priority,
        fallback_enabled=payload.fallback_enabled,
    )
    return prof


@app.delete("/api/llm/profiles/{name}")
async def api_llm_profiles_delete(name: str, _: str = Depends(_check_auth)):
    store = await _get_store()
    existing = await store.get_llm_profile(name)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"profile not found: {name}")
    if existing.get("is_active"):
        raise HTTPException(
            status_code=409, detail="cannot delete active profile; switch first"
        )
    await store.delete_llm_profile(name)
    return {"deleted": True, "name": name}


@app.post("/api/llm/profiles/{name}/test")
async def api_llm_profiles_test(name: str, _: str = Depends(_check_auth)):
    """Dry-run 校验：用该 profile 的 key 真的发一次最小 ping（按 provider）。

    失败抛 502（key 错/网络不通/模型不存在），成功返回 {"ok": True, "latency_ms": ...}。
    不会切换 active profile。
    """
    store = await _get_store()
    prof = await store.get_llm_profile(name)
    if prof is None:
        raise HTTPException(status_code=404, detail=f"profile not found: {name}")
    api_key = await store.get_llm_profile_secret(name)
    if not api_key:
        raise HTTPException(status_code=400, detail=f"profile {name} has no api_key")
    timeout = min(float(prof["timeout"]), 15.0)  # 测试给短超时
    provider = build_provider(
        prof["provider"], model=prof["model"], base_url=(prof.get("base_url") or None),
        api_key=api_key, timeout=timeout,
    )
    t0 = time.monotonic()
    try:
        # cap 到 256 token 防 8k 浪费
        max_tokens = min(int(prof.get("max_tokens", 1024) or 1024), 256)
        await provider.ping(max_tokens=max_tokens)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"profile test failed: {type(e).__name__}: {e}",
        ) from e
    finally:
        try:
            await provider.close()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "latency_ms": int((time.monotonic() - t0) * 1000)}


@app.post("/api/llm/profiles/{name}/activate")
async def api_llm_profiles_activate(
    name: str, user: str = Depends(_check_auth)
):
    """把 is_active 标志切到 name，并通过命令队列通知 engine 热替换。

    注意：DB 标志切换是同步的（前端能立刻看到 is_active=true）；
    engine 的 LLMClient 热替换是异步的，前端轮询 /api/llm/status 看 active.version
    变化（后续阶段 3 会让 active 返回带 version）。
    """
    store = await _get_store()
    prof = await store.get_llm_profile(name)
    if prof is None:
        raise HTTPException(status_code=404, detail=f"profile not found: {name}")
    await store.activate_llm_profile(name)
    cmd_id = await store.enqueue_command(
        "SWITCH_LLM_PROFILE", arg=name, source=f"web:{user}"
    )
    logger.warning(
        "llm profile switch queued: target={} by={} id={}", name, user, cmd_id
    )
    return {"queued": True, "id": cmd_id, "name": name,
            "note": "engine 将尽快热替换 LLMClient"}


# ---------- WebSocket：实时推送 ----------
@app.websocket("/ws/market")
async def ws_market(websocket: WebSocket):
    """实时行情 WS：按 source/symbol/timeframe 推 ticker 与最新 K 线。

    Query:
      source=mainnet|testnet
      symbol=BTCUSDT
      timeframe=1m|5m|...
    """
    if not _ws_authorized(websocket):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    symbols = await _registered_symbols()
    symbol = normalize_symbol(websocket.query_params.get("symbol") or (symbols[0] if symbols else ""))
    if symbol not in symbols:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    timeframe = websocket.query_params.get("timeframe") or _settings.llm.kline_interval
    source = _norm_source(websocket.query_params.get("source"))

    await websocket.accept()
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)
    tasks: list[asyncio.Task] = []

    async def _ticker_loop():
        feed = await _feeds.get(source)
        async for t in feed.watch_ticker(symbol):
            await queue.put({
                "type": "ticker",
                "symbol": symbol,
                "source": source,
                "last": t.get("last"),
                "mark": t.get("mark") or (t.get("info") or {}).get("markPrice"),
                "change_24h_pct": t.get("percentage"),
                "ts": t.get("timestamp"),
            })

    async def _kline_loop():
        feed = await _feeds.get(source)
        async for k in feed.watch_ohlcv(symbol, timeframe):
            await queue.put({
                "type": "kline",
                "symbol": symbol,
                "source": source,
                "timeframe": timeframe,
                "kline": k,
            })

    async def _producer(name: str, fn):
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await queue.put({"type": "error", "source": name, "message": str(e)})

    tasks.append(asyncio.create_task(_producer("ticker", _ticker_loop)))
    tasks.append(asyncio.create_task(_producer("kline", _kline_loop)))
    try:
        while True:
            msg = await queue.get()
            if msg.get("type") == "error":
                raise RuntimeError(f"{msg.get('source')} stream failed: {msg.get('message')}")
            await websocket.send_json(msg)
    except WebSocketDisconnect:
        return
    except Exception as e:
        logger.warning("market ws error {} {} [{}]: {}", symbol, timeframe, source, e)
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@app.websocket("/ws")
async def ws_stream(websocket: WebSocket):
    """每 push_interval 秒推送一帧聚合状态。

    鉴权：浏览器对同源 WS 握手会自动带上已建立的 Basic 凭证；这里再校验一次。
    """
    # 复用 HTTP Basic：从握手头解析
    if not _ws_authorized(websocket):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    push_interval = float(os.environ.get("WEB_PUSH_INTERVAL", "1"))
    try:
        while True:
            await websocket.send_json(await _status_summary())
            await asyncio.sleep(push_interval)
    except WebSocketDisconnect:
        return
    except Exception as e:
        logger.warning("ws stream error: {}", e)
        await websocket.close()


def _ws_authorized(websocket: WebSocket) -> bool:
    import base64
    if not _WEB_PASSWORD:
        return False
    session_user = _session_user_from_cookie(websocket.cookies.get(_SESSION_COOKIE, ""))
    if session_user:
        return True
    cookie = websocket.cookies.get("binance_trade_ws", "")
    if cookie and secrets.compare_digest(cookie, _ws_auth_cookie_value()):
        return True
    auth = websocket.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        user, _, pwd = decoded.partition(":")
    except Exception:
        return False
    return (secrets.compare_digest(user, _WEB_USER)
            and secrets.compare_digest(pwd, _WEB_PASSWORD))


@app.get("/healthz")
async def healthz():
    """健康检查（无需鉴权），供 systemd/nginx 探活。"""
    return JSONResponse({"status": "ok"})


@app.on_event("startup")
async def _ensure_schema() -> None:
    """确保数据库表已建（即使交易进程尚未运行过），避免只读查询命中缺表。"""
    try:
        await _get_store()  # Store.connect() 会 create_all（幂等）
    except Exception as e:
        logger.warning("ensure schema on startup failed: {}", e)


# ---------- 静态前端（构建产物）----------
_FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "frontend", "dist")
if os.path.isdir(_FRONTEND_DIST):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIST, html=True), name="frontend")
else:
    logger.warning("frontend dist 未构建：{}（先 npm run build）", _FRONTEND_DIST)


def main() -> None:
    import uvicorn
    host = os.environ.get("WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("WEB_PORT", "8000"))
    if not _WEB_PASSWORD:
        logger.warning("⚠️  WEB_PASSWORD 未设置，所有接口将拒绝访问。请在 .env 配置。")
    logger.info("starting web dashboard on {}:{}", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()


# <!-- APPEND_ENDPOINTS -->
