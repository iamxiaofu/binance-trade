"""Telegram 告警：关键事件推送（可开关）。

- 通过 httpx 异步调用 Telegram Bot API，不引入额外 SDK。
- ``telegram_enabled=false`` 时所有 send 静默成功（无副作用）。
- 任何网络错误都被吞掉并记日志：告警失败绝不能影响交易主流程。
- 事件类型用 ``notify_events`` 白名单过滤，未列入的事件不推送。
"""
from __future__ import annotations

from enum import Enum

import httpx
from loguru import logger

from src.config.schema import NotifyConfig


class Event(str, Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    REJECT = "REJECT"
    CIRCUIT_BREAK = "CIRCUIT_BREAK"
    ERROR = "ERROR"
    KILL_SWITCH = "KILL_SWITCH"
    HEARTBEAT = "HEARTBEAT"


_EMOJI = {
    Event.OPEN: "🟢",
    Event.CLOSE: "🔵",
    Event.REJECT: "⛔",
    Event.CIRCUIT_BREAK: "🚨",
    Event.ERROR: "❌",
    Event.KILL_SWITCH: "🛑",
    Event.HEARTBEAT: "💓",
}


class Notifier:
    """Telegram 告警器。enabled=False 时为空操作。"""

    _API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(
        self,
        cfg: NotifyConfig,
        bot_token: str | None = None,
        chat_id: str | None = None,
    ):
        self._cfg = cfg
        self._token = bot_token
        self._chat_id = chat_id
        self._enabled = bool(cfg.telegram_enabled and bot_token and chat_id)
        # 允许的事件集合；为空表示全部允许
        self._allowed = {e.upper() for e in cfg.notify_events} if cfg.notify_events else None
        self._client: httpx.AsyncClient | None = None
        if self._enabled:
            self._client = httpx.AsyncClient(timeout=10.0)
        else:
            logger.info("notifier disabled (telegram_enabled or creds missing)")

    def _should_send(self, event: Event) -> bool:
        if not self._enabled:
            return False
        if self._allowed is None:
            return True
        return event.value in self._allowed

    async def send(self, event: Event, message: str) -> bool:
        """推送一条事件。返回是否实际发送成功。永不抛出。"""
        if not self._should_send(event):
            return False
        text = f"{_EMOJI.get(event, '')} [{event.value}] {message}"
        try:
            assert self._client is not None
            resp = await self._client.post(
                self._API.format(token=self._token),
                json={"chat_id": self._chat_id, "text": text},
            )
            if resp.status_code != 200:
                logger.warning("telegram send non-200: {} {}", resp.status_code, resp.text[:200])
                return False
            return True
        except Exception as e:  # 告警失败不影响主流程
            logger.warning("telegram send failed: {}", e)
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
