"""Native Binance USD-M private User Data Stream transport."""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from loguru import logger

from src.config.schema import Settings
from src.exchange.client import ExchangeClient
from src.exchange.events import ExchangeEvent, private_event

EventHandler = Callable[[ExchangeEvent], Awaitable[None]]
HealthHandler = Callable[[dict[str, Any]], Awaitable[None]]


class BinanceUserDataStream:
    def __init__(
        self,
        client: ExchangeClient,
        settings: Settings,
        on_event: EventHandler,
        on_health: HealthHandler,
    ):
        self._client = client
        self._settings = settings
        self._cfg = settings.user_stream
        self._on_event = on_event
        self._on_health = on_health
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._listen_key = ""
        self._session_id = ""
        self._keepalive_failed = asyncio.Event()

    async def start(self) -> None:
        if not self._cfg.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="binance-user-data-stream")

    async def wait_connected(self, timeout: float = 20.0) -> None:
        if not self._cfg.enabled:
            return
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

    async def close(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._close_listen_key()

    async def _emit_health(self, status: str, reason: str = "", **extra: Any) -> None:
        await self._on_health({
            "status": status,
            "reason": reason,
            "session_id": self._session_id,
            "listen_key_hash": (
                hashlib.sha256(self._listen_key.encode()).hexdigest() if self._listen_key else ""
            ),
            "ts_ms": int(time.time() * 1000),
            **extra,
        })

    async def _create_listen_key(self) -> str:
        raw = await self._client.raw.fapiPrivatePostListenKey()
        key = str(raw.get("listenKey") or "")
        if not key:
            raise RuntimeError("Binance returned an empty listenKey")
        return key

    async def _keepalive(self) -> None:
        try:
            while not self._stop.is_set():
                await asyncio.sleep(self._cfg.keepalive_seconds)
                await self._client.raw.fapiPrivatePutListenKey({"listenKey": self._listen_key})
                await self._emit_health("LIVE", keepalive_at_ms=int(time.time() * 1000))
        except asyncio.CancelledError:
            raise
        except Exception:
            self._keepalive_failed.set()
            raise

    async def _close_listen_key(self) -> None:
        if not self._listen_key:
            return
        try:
            await self._client.raw.fapiPrivateDeleteListenKey({"listenKey": self._listen_key})
        except Exception as exc:
            logger.debug("close listenKey skipped: {}", exc)
        self._listen_key = ""

    def _ws_url(self) -> str:
        configured = (self._cfg.private_ws_base_url or "").rstrip("/")
        if configured:
            return f"{configured}/{self._listen_key}"
        if self._settings.is_mainnet:
            return f"wss://fstream.binance.com/private/ws/{self._listen_key}"
        return f"wss://fstream.binancefuture.com/ws/{self._listen_key}"

    async def _run_session(self) -> None:
        self._listen_key = await self._create_listen_key()
        self._session_id = uuid.uuid4().hex
        self._keepalive_failed.clear()
        url = self._ws_url()
        keepalive = asyncio.create_task(self._keepalive(), name="listen-key-keepalive")
        started = time.monotonic()
        try:
            async with websockets.connect(
                url,
                ping_interval=None,
                close_timeout=5,
                max_queue=self._cfg.startup_buffer_limit,
            ) as ws:
                self._connected.set()
                await self._emit_health("LIVE", connected_at_ms=int(time.time() * 1000))
                while not self._stop.is_set():
                    if self._keepalive_failed.is_set():
                        raise RuntimeError("listenKey keepalive failed")
                    remaining = self._cfg.rotate_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        await self._emit_health("RESYNCING", "proactive connection rotation")
                        return
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 600))
                    except asyncio.TimeoutError:
                        pong = await ws.ping()
                        await asyncio.wait_for(pong, timeout=10)
                        continue
                    payload = json.loads(raw)
                    event = private_event(payload, self._session_id)
                    await self._on_event(event)
                    if event.event_type == "listenKeyExpired":
                        raise RuntimeError("listenKey expired")
        finally:
            self._connected.clear()
            keepalive.cancel()
            try:
                await keepalive
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("listenKey keepalive task failed: {}", exc)
            await self._close_listen_key()

    async def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._run_session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected.clear()
                await self._emit_health("DISCONNECTED", str(exc))
                delay = min(self._cfg.reconnect_max_seconds, 2 ** min(attempt, 5))
                delay += random.random()
                attempt += 1
                logger.warning("private user stream disconnected: {}; reconnect {:.1f}s", exc, delay)
                await asyncio.sleep(delay)
