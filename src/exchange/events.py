"""Canonical private-account events shared by websocket and REST reconciliation."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ExchangeEvent:
    event_type: str
    payload: dict[str, Any]
    event_time_ms: int = 0
    transaction_time_ms: int = 0
    source: str = "stream"
    session_id: str = ""
    received_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    event_key: str = ""

    def __post_init__(self) -> None:
        if self.event_key:
            return
        body = json.dumps(self.payload, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(
            f"{self.source}:{self.session_id}:{self.event_type}:{body}".encode()
        ).hexdigest()
        object.__setattr__(self, "event_key", digest)


def private_event(payload: dict[str, Any], session_id: str) -> ExchangeEvent:
    return ExchangeEvent(
        event_type=str(payload.get("e") or "UNKNOWN"),
        payload=payload,
        event_time_ms=int(payload.get("E") or 0),
        transaction_time_ms=int(payload.get("T") or 0),
        source="stream",
        session_id=session_id,
    )


def rest_snapshot_event(
    *,
    positions: list[dict[str, Any]],
    open_orders: list[dict[str, Any]],
    balance: dict[str, Any],
    reason: str,
) -> ExchangeEvent:
    now = int(time.time() * 1000)
    return ExchangeEvent(
        event_type="REST_ACCOUNT_SNAPSHOT",
        payload={
            "positions": positions,
            "open_orders": open_orders,
            "balance": balance,
            "reason": reason,
        },
        event_time_ms=now,
        transaction_time_ms=now,
        source="rest",
        event_key=f"rest:{reason}:{now}",
    )
