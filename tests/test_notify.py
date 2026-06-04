"""notify/telegram.py 测试：禁用静默、事件白名单、失败不抛出。"""
from __future__ import annotations

import pytest

from src.config.schema import NotifyConfig
from src.notify.telegram import Event, Notifier


async def test_disabled_when_flag_off():
    n = Notifier(NotifyConfig(telegram_enabled=False), "tok", "chat")
    assert await n.send(Event.OPEN, "hi") is False
    await n.close()


async def test_disabled_when_creds_missing():
    n = Notifier(NotifyConfig(telegram_enabled=True), None, None)
    assert await n.send(Event.OPEN, "hi") is False
    await n.close()


async def test_event_whitelist(monkeypatch):
    cfg = NotifyConfig(telegram_enabled=True, notify_events=["OPEN"])
    n = Notifier(cfg, "tok", "chat")

    sent: list[str] = []

    class FakeResp:
        status_code = 200
        text = "ok"

    async def fake_post(url, json):
        sent.append(json["text"])
        return FakeResp()

    monkeypatch.setattr(n._client, "post", fake_post)
    assert await n.send(Event.OPEN, "opened") is True       # 白名单内
    assert await n.send(Event.REJECT, "rejected") is False  # 不在白名单
    assert len(sent) == 1
    await n.close()


async def test_send_swallows_exceptions(monkeypatch):
    n = Notifier(NotifyConfig(telegram_enabled=True), "tok", "chat")

    async def boom(url, json):
        raise RuntimeError("network down")

    monkeypatch.setattr(n._client, "post", boom)
    assert await n.send(Event.ERROR, "x") is False  # 不抛出
    await n.close()
