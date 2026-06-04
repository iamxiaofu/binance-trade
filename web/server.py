"""Web 后端：FastAPI 只读看板 + WebSocket 推送 + 受控操作端点。

安全与解耦原则：
- 独立进程，与交易主进程分离；只读 SQLite（status.py）+ 只读交易所行情。
- 操作类命令（Kill Switch / 暂停 / dry_run 切换）只写 control_commands 表，
  由交易进程每周期消费执行；web 绝不直接碰交易所下单。
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

from fastapi import Depends, FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from loguru import logger

from src.config.loader import load_config
from src.exchange.client import ExchangeClient
from src.exchange.orders import normalize_condition_order
from src.exchange.positions import normalize_position
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


async def _effective_dry_run() -> tuple[bool, str]:
    try:
        store = await _get_store()
        raw = await store.get_runtime_setting("execution.dry_run")
        if raw is not None:
            return _parse_bool_setting(raw, _settings.execution.dry_run), "runtime"
    except Exception as e:
        logger.warning("runtime dry_run unavailable, fallback to config: {}", e)
    return _settings.execution.dry_run, "config"


async def _live_positions_snapshot() -> dict[str, Any]:
    """Fetch current exchange positions with a short cache for dashboard pushes."""
    now = int(time.time() * 1000)
    async with _positions_lock:
        cached_ts = int(_positions_cache.get("ts_ms") or 0)
        if cached_ts and now - cached_ts <= _POSITIONS_TTL_MS:
            return dict(_positions_cache)

        try:
            client = await _get_market()
            raw_positions = await client.fetch_positions(_settings.symbols)
            positions = []
            for raw in raw_positions:
                pos = normalize_position(raw)
                if pos["contracts"] > 0:
                    positions.append(pos)
            condition_orders, condition_error = await _live_condition_orders(
                client, [p["symbol"] for p in positions]
            )
            _attach_protection_orders(positions, condition_orders)
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
            recent = await client.fetch_condition_orders(symbol, limit=20)
            for raw in recent:
                order = normalize_condition_order(raw)
                if order["kind"] in ("SL", "TP"):
                    merged[order["id"] or f"{symbol}:{order['kind']}:{order['ts_ms']}"] = order
        except Exception as e:
            errors.append(f"{symbol} history: {e}")
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


def _select_protection_order(orders: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    matching = [o for o in orders if o["kind"] == kind]
    active = [o for o in matching if o["status"] == "placed"]
    pool = active or matching
    if not pool:
        return None
    return sorted(pool, key=lambda x: x.get("ts_ms") or 0, reverse=True)[0]


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
async def api_decisions(limit: int = 100, _: str = Depends(_check_auth)):
    return st.recent_decisions(_DB, min(limit, 500))


@app.get("/api/decisions/{decision_id}")
async def api_decision_detail(decision_id: int, _: str = Depends(_check_auth)):
    row = st.decision_detail(_DB, decision_id)
    if row is None:
        raise HTTPException(status_code=404, detail="decision not found")
    return row


@app.get("/api/orders")
async def api_orders(limit: int = 100, _: str = Depends(_check_auth)):
    return st.recent_orders(_DB, min(limit, 500))


@app.get("/api/rejects")
async def api_rejects(limit: int = 100, _: str = Depends(_check_auth)):
    return st.recent_rejects(_DB, min(limit, 500))


@app.get("/api/pnl")
async def api_pnl(_: str = Depends(_check_auth)):
    return st.pnl_stats(_DB)


@app.get("/api/equity")
async def api_equity(limit: int = 500, _: str = Depends(_check_auth)):
    return st.balance_history(_DB, min(limit, 2000))


@app.get("/api/commands")
async def api_commands(limit: int = 50, _: str = Depends(_check_auth)):
    return st.recent_commands(_DB, min(limit, 200))


@app.get("/api/config")
async def api_config(_: str = Depends(_check_auth)):
    """暴露非敏感运行配置，供前端展示风控阈值等。"""
    s = _settings
    dry_run, dry_run_source = await _effective_dry_run()
    return {
        "mode": s.mode.value,
        "symbols": s.symbols,
        "dry_run": dry_run,
        "dry_run_source": dry_run_source,
        "dry_run_config": s.execution.dry_run,
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
    symbol = symbol.upper()
    if symbol not in _settings.symbols:
        raise HTTPException(status_code=400, detail=f"symbol not configured: {symbol}")
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
    symbol = symbol.upper()
    if symbol not in _settings.symbols:
        raise HTTPException(status_code=400, detail=f"symbol not configured: {symbol}")
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
    "SET_DRY_RUN",
    "REPAIR_SL_TP",
    "CANCEL_AND_FLATTEN",
    "STOP_ENGINE",
}


@app.post("/api/command/{name}")
async def api_command(name: str, arg: str = "", user: str = Depends(_check_auth)):
    """下发控制命令。交易进程每周期消费执行（最多一个周期延迟）。"""
    name = name.upper()
    if name not in _ALLOWED_COMMANDS:
        raise HTTPException(status_code=400, detail=f"unknown command: {name}")
    store = await _get_store()
    cmd_id = await store.enqueue_command(name, arg=arg, source=f"web:{user}")
    logger.warning("web command queued: {} arg={} by={} id={}", name, arg, user, cmd_id)
    return {"queued": True, "id": cmd_id, "command": name,
            "note": "交易进程将在下个周期内执行"}


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

    symbol = (websocket.query_params.get("symbol") or _settings.symbols[0]).upper()
    if symbol not in _settings.symbols:
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
