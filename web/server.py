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
import os
import secrets
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from loguru import logger

from src.config.loader import load_config
from src.exchange.client import ExchangeClient
from src.exchange.orders import normalize_condition_order
from src.exchange.positions import normalize_position, normalize_symbol
from src.features.indicators import compute_snapshot
from src.store.repo import Store
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

app = FastAPI(title="binance-trade dashboard", docs_url=None, redoc_url=None)
_security = HTTPBasic()

# 只读行情客户端（懒加载，单例）
_market_client: ExchangeClient | None = None
_market_lock = asyncio.Lock()
# 交易所实时持仓缓存：WS 每秒推送，但私有持仓接口不需要每帧都打交易所。
_positions_lock = asyncio.Lock()
_positions_cache: dict[str, Any] = {
    "ts_ms": 0,
    "positions": [],
    "condition_orders": [],
    "error": "",
    "condition_error": "",
}
_POSITIONS_TTL_MS = int(os.environ.get("WEB_POSITIONS_TTL_MS", "2000"))
# 行情数据源注册表：mainnet/testnet 双源（REST + WS），供看板行情用
_feeds = MarketFeedRegistry()
# 默认行情源跟随交易模式；需要单独看主网时可用 WEB_MARKET_SOURCE=mainnet 覆盖。
_SOURCE_ENV = os.environ.get("WEB_MARKET_SOURCE", _settings.mode.value).lower()
_DEFAULT_SOURCE = _SOURCE_ENV if _SOURCE_ENV in ("mainnet", "testnet") else _settings.mode.value
# 控制命令写入用的 Store（懒加载）
_store: Store | None = None


def _ws_auth_cookie_value() -> str:
    if not _WEB_PASSWORD:
        return ""
    return hashlib.sha256(f"{_WEB_USER}:{_WEB_PASSWORD}".encode()).hexdigest()


def _check_auth(
    response: Response,
    credentials: HTTPBasicCredentials = Depends(_security),
) -> str:
    """Basic Auth 校验。用 compare_digest 防时序攻击。"""
    if not _WEB_PASSWORD:
        # 未配置密码 → 拒绝一切访问，避免裸奔
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WEB_PASSWORD 未配置，拒绝访问。请在 .env 设置 WEB_USER/WEB_PASSWORD。",
        )
    ok_user = secrets.compare_digest(credentials.username, _WEB_USER)
    ok_pass = secrets.compare_digest(credentials.password, _WEB_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证失败",
            headers={"WWW-Authenticate": "Basic"},
        )
    response.set_cookie(
        "binance_trade_ws",
        _ws_auth_cookie_value(),
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
    )
    return credentials.username


async def _get_store() -> Store:
    global _store
    if _store is None:
        _store = Store(_DB)
        await _store.connect()
        await _store.sync_config_symbols(_settings.symbols)
    return _store


async def _get_market() -> ExchangeClient:
    global _market_client
    async with _market_lock:
        if _market_client is None:
            client = ExchangeClient(_settings, _creds)
            await client.load_markets()
            _market_client = client
    return _market_client


def _parse_bool_setting(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


async def _effective_strategy_paused() -> tuple[bool, str]:
    try:
        store = await _get_store()
        raw = await store.get_runtime_setting("strategy.paused")
        if raw is not None:
            return _parse_bool_setting(raw, False), "runtime"
    except Exception as e:
        logger.warning("runtime strategy status unavailable, fallback to running: {}", e)
    return False, "default"


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
            }
            for symbol in _settings.symbols
        ]


async def _registered_symbols() -> list[str]:
    rows = await _symbol_rows()
    return [normalize_symbol(row["symbol"]) for row in rows]


async def _live_positions_snapshot() -> dict[str, Any]:
    """Fetch current exchange positions with a short cache for dashboard pushes."""
    now = int(time.time() * 1000)
    async with _positions_lock:
        cached_ts = int(_positions_cache.get("ts_ms") or 0)
        if cached_ts and now - cached_ts <= _POSITIONS_TTL_MS:
            return dict(_positions_cache)

        try:
            client = await _get_market()
            raw_positions = await client.fetch_positions(await _registered_symbols())
            positions = []
            for raw in raw_positions:
                pos = normalize_position(raw)
                if pos["contracts"] > 0:
                    positions.append(pos)
            condition_orders, condition_error = await _live_condition_orders(
                client, [p["symbol"] for p in positions]
            )
            _attach_protection_orders(positions, condition_orders)
            _attach_local_trade_metadata(positions)
            _positions_cache.update({
                "ts_ms": int(time.time() * 1000),
                "positions": positions,
                "condition_orders": condition_orders,
                "error": "",
                "condition_error": condition_error,
            })
        except Exception as e:
            _positions_cache["error"] = str(e)
            raise
        return dict(_positions_cache)


async def _live_condition_orders(
    client: ExchangeClient,
    symbols: list[str],
) -> tuple[list[dict[str, Any]], str]:
    merged: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for symbol in symbols:
        try:
            open_orders = await client.fetch_open_condition_orders(symbol)
            for raw in open_orders:
                order = normalize_condition_order(raw)
                if order["kind"] in ("SL", "TP"):
                    order["status"] = "placed"
                    merged[order["id"] or f"{symbol}:{order['kind']}:{order['ts_ms']}"] = order
        except Exception as e:
            errors.append(f"{symbol} open: {e}")
    orders = sorted(merged.values(), key=lambda x: (x["symbol"], x["kind"], -(x["ts_ms"] or 0)))
    return orders, "; ".join(errors)


def _attach_protection_orders(positions: list[dict], orders: list[dict[str, Any]]) -> None:
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for order in orders:
        by_symbol.setdefault(order["symbol"], []).append(order)

    for pos in positions:
        related = by_symbol.get(pos["symbol"], [])
        protection: dict[str, Any] = {
            "sl": _select_protection_order(related, "SL"),
            "tp": _select_protection_order(related, "TP"),
        }
        protection["sl_active"] = (protection["sl"] or {}).get("status") == "placed"
        protection["tp_active"] = (protection["tp"] or {}).get("status") == "placed"
        protection["missing_sl"] = not protection["sl_active"]
        protection["missing_tp"] = not protection["tp_active"]
        pos["protection_orders"] = related
        pos["protection"] = protection


def _attach_local_trade_metadata(positions: list[dict]) -> None:
    meta = st.open_trade_metadata(_DB)
    for pos in positions:
        item = meta.get(pos.get("symbol") or "")
        if not item:
            continue
        pos.update(item)
        if not pos.get("leverage") and item.get("local_leverage"):
            pos["leverage"] = item["local_leverage"]


def _select_protection_order(orders: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    active = [o for o in orders if o["kind"] == kind and o["status"] == "placed"]
    if not active:
        return None
    return sorted(active, key=lambda x: x.get("ts_ms") or 0, reverse=True)[0]


async def _status_summary() -> dict[str, Any]:
    summary = st.status_summary(_DB)
    try:
        live = await _live_positions_snapshot()
        summary["positions"] = live["positions"]
        summary["positions_source"] = "exchange"
        summary["positions_error"] = live.get("error", "")
        summary["condition_orders"] = live.get("condition_orders", [])
        summary["condition_orders_error"] = live.get("condition_error", "")
        summary["positions_synced_at_ms"] = live.get("ts_ms")
    except Exception as e:
        logger.warning("live positions unavailable, fallback to db snapshot: {}", e)
        summary["positions_source"] = "db_snapshot"
        summary["positions_error"] = str(e)
        summary["condition_orders"] = []
        summary["condition_orders_error"] = ""
        summary["positions_synced_at_ms"] = None
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


# ---------- REST：只读数据 ----------
@app.get("/api/summary")
async def api_summary(_: str = Depends(_check_auth)):
    return await _status_summary()


@app.get("/api/positions")
async def api_positions(_: str = Depends(_check_auth)):
    try:
        live = await _live_positions_snapshot()
        return live["positions"]
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


@app.get("/api/trades")
async def api_trades(
    symbol: list[str] = Query(default_factory=list),
    direction: list[str] = Query(default_factory=list),
    status: list[str] = Query(default_factory=list),
    exit_reason: list[str] = Query(default_factory=list),
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
        live = await _live_positions_snapshot()
        stats["day_unrealized_pnl"] = sum(
            float(position.get("unrealized_pnl") or 0.0)
            for position in live.get("positions", [])
        )
        stats["unrealized_source"] = "exchange"
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
    symbol_enabled = await _effective_symbol_enabled()
    symbols_state = await _symbol_rows()
    symbols = [row["symbol"] for row in symbols_state]
    return {
        "mode": s.mode.value,
        "db_path": s.storage.db_path,
        "symbols": symbols,
        "strategy_paused": strategy_paused,
        "strategy_status_source": strategy_status_source,
        "symbol_enabled": symbol_enabled,
        "symbols_state": symbols_state,
        "cycle_interval": s.cycle.interval,
        # 看板默认行情源（mainnet 真实价 / testnet 沙盒），供前端初始化源切换
        "market_source": _DEFAULT_SOURCE,
        "risk": {
            "max_leverage": s.risk.max_leverage,
            # 保证金/亏损限额按「账户权益」动态缩放
            "max_order_margin_pct": s.risk.max_order_margin_pct,
            "max_symbol_margin_pct": s.risk.max_symbol_margin_pct,
            "max_total_margin_pct": s.risk.max_total_margin_pct,
            "max_loss_per_trade_pct": s.risk.max_loss_per_trade_pct,
            "max_drawdown_pct": s.risk.max_drawdown_pct,
            "daily_max_loss_pct": s.risk.daily_max_loss_pct,
            "min_confidence": s.risk.min_confidence,
        },
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
    "CANCEL_AND_FLATTEN",
    "STOP_ENGINE",
    "SWITCH_LLM_PROFILE",
}


@app.post("/api/command/{name}")
async def api_command(name: str, arg: str = "", user: str = Depends(_check_auth)):
    """下发控制命令。交易进程快速消费执行。"""
    name = name.upper()
    if name not in _ALLOWED_COMMANDS:
        raise HTTPException(status_code=400, detail=f"unknown command: {name}")
    store = await _get_store()
    cmd_id = await store.enqueue_command(name, arg=arg, source=f"web:{user}")
    logger.warning("web command queued: {} arg={} by={} id={}", name, arg, user, cmd_id)
    return {"queued": True, "id": cmd_id, "command": name,
            "note": "交易进程将尽快消费执行"}


# ---------- LLM profile 管理 ----------
# 全部写 llm_profiles 表 + keyring；激活通过现有命令队列触发 engine 热替换。
# 设计要点：
# - list / get / activate / test 永远不返回 key 明文（响应里没有这个字段）。
# - 创建/更新时只接受 api_key 一次性的明文（POST/PUT body），入库前 keyring.set，
#   写库只放 keyring_ref/ciphertext。
# - keyring 后端不可用时，写操作返回 503 + 明确 hint，前端用 banner 提示。

from pydantic import BaseModel, Field  # noqa: E402
from src.llm.keyring_store import (  # noqa: E402
    get_keyring_store,
    KeyringUnavailable,
)


class _LLMProfileUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    provider: str = Field(default="anthropic", pattern="^anthropic$")
    model: str = Field(min_length=1, max_length=128)
    base_url: str | None = None
    timeout: float = Field(default=60.0, gt=0)
    max_tokens: int = Field(default=1024, gt=0, le=512000)
    max_retries: int = Field(default=2, ge=0, le=5)
    # 留空=不更新（PUT 时）；POST 时必填
    api_key: str = Field(default="", max_length=512)


def _keyring_status() -> dict:
    _ks, status = get_keyring_store()
    return status


def _check_keyring_available() -> None:
    """写操作前置：keyring 后端不可用直接 503。"""
    s = _keyring_status()
    if not s.get("available"):
        raise HTTPException(
            status_code=503,
            detail={
                "error": "keyring_unavailable",
                "backend": s.get("backend"),
                "hint": s.get("hint"),
            },
        )


@app.get("/api/llm/status")
async def api_llm_status(_: str = Depends(_check_auth)):
    """前端顶部 banner + 切换轮询用。"""
    s = _keyring_status()
    store = await _get_store()
    active = await store.get_active_llm_profile()
    rt = await store.runtime_settings()
    # engine 进程热替换后会写 llm.active_version / llm.active_name / llm.active_source
    return {
        "keyring": s,
        "active": active,
        "switching_supported": s.get("available", False),
        "engine": {
            "active_name": rt.get("llm.active_name", ""),
            "active_version": int(rt.get("llm.active_version", "0") or "0"),
            "active_source": rt.get("llm.active_source", ""),
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
    _check_keyring_available()
    if not payload.api_key.strip():
        raise HTTPException(
            status_code=400, detail="api_key is required when creating a profile"
        )
    store = await _get_store()
    existing = await store.get_llm_profile(payload.name)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"profile exists: {payload.name}")
    ks, _ = get_keyring_store()
    try:
        ref = ks.set(payload.name, payload.api_key)
    except KeyringUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    try:
        prof = await store.upsert_llm_profile(
            name=payload.name,
            provider=payload.provider,
            model=payload.model,
            base_url=(payload.base_url or None),
            timeout=payload.timeout,
            max_tokens=payload.max_tokens,
            max_retries=payload.max_retries,
            keyring_ref=ref,
        )
    except Exception:
        # 落库失败时回滚 keyring 项，避免孤儿。
        try:
            ks.delete(ref)
        except Exception:  # noqa: BLE001
            pass
        raise
    return prof


@app.put("/api/llm/profiles/{name}")
async def api_llm_profiles_update(
    name: str, payload: _LLMProfileUpsert, _: str = Depends(_check_auth)
):
    _check_keyring_available()
    store = await _get_store()
    existing = await store.get_llm_profile(name)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"profile not found: {name}")
    # 如果新 api_key 留空 → 保留旧 keyring_ref；非空 → 写新 keyring。
    new_ref = ""
    if payload.api_key.strip():
        ks, _ = get_keyring_store()
        old_ref = existing.get("keyring_ref") or ""
        try:
            new_ref = ks.set(name, payload.api_key)
        except KeyringUnavailable as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        try:
            prof = await store.upsert_llm_profile(
                name=name,
                provider=payload.provider,
                model=payload.model,
                base_url=(payload.base_url or None),
                timeout=payload.timeout,
                max_tokens=payload.max_tokens,
                max_retries=payload.max_retries,
                keyring_ref=new_ref,
            )
        except Exception:
            try:
                ks.delete(new_ref)
            except Exception:  # noqa: BLE001
                pass
            raise
        # 旧 key 删掉（仅 keyring 模式有意义；fernet 模式下 ref 是密文，新覆盖即可）
        if old_ref and old_ref != new_ref:
            try:
                ks.delete(old_ref)
            except Exception:  # noqa: BLE001
                logger.warning("failed to delete old keyring ref: {}", old_ref)
    else:
        prof = await store.upsert_llm_profile(
            name=name,
            provider=payload.provider,
            model=payload.model,
            base_url=(payload.base_url or None),
            timeout=payload.timeout,
            max_tokens=payload.max_tokens,
            max_retries=payload.max_retries,
            keyring_ref="",
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
    ks, _ = get_keyring_store()
    old_ref = existing.get("keyring_ref") or ""
    if old_ref:
        try:
            ks.delete(old_ref)
        except Exception:  # noqa: BLE001
            logger.warning("failed to delete keyring ref: {}", old_ref)
    return {"deleted": True, "name": name}


@app.post("/api/llm/profiles/{name}/test")
async def api_llm_profiles_test(name: str, _: str = Depends(_check_auth)):
    """Dry-run 校验：用该 profile 的 key 真的发一次最小 ping。

    失败抛 502（key 错/网络不通/模型不存在），成功返回 {"ok": True, "latency_ms": ...}。
    不会切换 active profile。
    """
    store = await _get_store()
    prof = await store.get_llm_profile(name)
    if prof is None:
        raise HTTPException(status_code=404, detail=f"profile not found: {name}")
    ks, _ = get_keyring_store()
    try:
        api_key = ks.get(prof["keyring_ref"])
    except (KeyError, KeyringUnavailable) as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    # 真正的 ping：test 端点只关心 (model, base_url, timeout, api_key, max_tokens)，
    # 不依赖 LLMConfig 的工程参数 (kline_lookback / kline_interval / ...)，所以
    # 这里直接构造 AsyncAnthropic，避免 LLMConfig 必填字段缺失的问题。
    from anthropic import AsyncAnthropic  # type: ignore
    timeout = min(float(prof["timeout"]), 15.0)  # 测试给短超时
    kwargs = {"api_key": api_key, "timeout": timeout}
    base_url = prof.get("base_url") or None
    if base_url:
        kwargs["base_url"] = base_url
    client = AsyncAnthropic(**kwargs)
    t0 = time.monotonic()
    try:
        # 用 profile 自己的 max_tokens 做上限，但 cap 到 256 防 8k 浪费
        max_tokens = min(int(prof.get("max_tokens", 1024) or 1024), 256)
        await client.messages.create(
            model=prof["model"],
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": "ping"}],
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"profile test failed: {type(e).__name__}: {e}",
        ) from e
    finally:
        try:
            await client.close()
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
