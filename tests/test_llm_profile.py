"""LLM profile 管理（持久化 + keyring + 工厂）测试。"""
from __future__ import annotations

import asyncio
import os

import pytest

from src.config.schema import LLMConfig
from src.llm import keyring_store as kr
from src.llm.client import LLMClient
from src.store.repo import Store


@pytest.fixture
def fernet_env(monkeypatch):
    """强制走 Fernet 后端（headless CI 无 keyring）。"""
    monkeypatch.setenv("LLM_KEYRING_MASTER_KEY", "KXSIvd3t8TTEjuyz8A0daMTgpm4Ezcoix2BzelW-XxE=")
    kr.reset_for_test()
    yield
    kr.reset_for_test()


async def test_store_llm_profile_crud(tmp_path):
    db = str(tmp_path / "t.db")
    s = Store(db)
    await s.connect()
    try:
        # 初始空
        assert await s.list_llm_profiles() == []
        assert await s.get_active_llm_profile() is None
        # upsert
        await s.upsert_llm_profile(
            name="ikuncode", provider="anthropic", model="claude-opus-4-8",
            base_url="https://api.ikuncode.cc", timeout=60.0,
            max_tokens=1024, max_retries=2, keyring_ref="profile://keyring/ikuncode",
        )
        prof = await s.get_llm_profile("ikuncode")
        assert prof and prof["key_present"] and not prof["is_active"]
        # 重复 upsert：留空 keyring_ref → 保留旧
        await s.upsert_llm_profile(
            name="ikuncode", provider="anthropic", model="claude-opus-4-6",
            base_url="", timeout=80.0, max_tokens=2048, max_retries=3, keyring_ref="",
        )
        prof2 = await s.get_llm_profile("ikuncode")
        assert prof2["model"] == "claude-opus-4-6"
        assert prof2["keyring_ref"] == "profile://keyring/ikuncode"
        # 互斥激活
        await s.upsert_llm_profile(
            name="official", provider="anthropic", model="claude-opus-4-6",
            base_url="", timeout=60.0, max_tokens=1024, max_retries=2,
            keyring_ref="profile://keyring/official",
        )
        await s.activate_llm_profile("official")
        assert (await s.get_llm_profile("ikuncode"))["is_active"] is False
        assert (await s.get_llm_profile("official"))["is_active"] is True
        # 删 active 应抛错
        with pytest.raises(ValueError, match="cannot delete active"):
            await s.delete_llm_profile("official")
        # 删非 active 成功
        assert await s.delete_llm_profile("ikuncode") is True
        rows = await s.list_llm_profiles()
        assert [r["name"] for r in rows] == ["official"]
    finally:
        await s.close()


def test_fernet_roundtrip(fernet_env):
    ks, status = kr.get_keyring_store()
    assert status["backend"] == "fernet"
    assert status["available"] is True
    ref = ks.set("official", "sk-ant-FAKE")
    assert ks.get(ref) == "sk-ant-FAKE"


def test_fernet_ciphertext_does_not_leak_plaintext(fernet_env):
    ks, _ = kr.get_keyring_store()
    plaintext = "sk-ant-THIS-IS-THE-SECRET"
    ref = ks.set("official", plaintext)
    assert plaintext not in ref
    # Fernet 密文一定不等于明文
    assert ref != plaintext


def test_unavailable_when_no_backend(monkeypatch):
    monkeypatch.delenv("LLM_KEYRING_MASTER_KEY", raising=False)
    kr.reset_for_test()
    ks, status = kr.get_keyring_store()
    assert status["backend"] == "unavailable"
    assert status["available"] is False
    with pytest.raises(kr.KeyringUnavailable):
        ks.set("x", "y")


def test_llm_client_from_profile_uses_profile_values(fernet_env):
    """from_profile 工厂应该用 profile 里的 model/timeout/base_url，而不是 yaml 默认。"""
    cfg = LLMConfig(
        model="claude-opus-4-8", timeout=60, max_tokens=1024,
        max_retries=2, kline_lookback=100,
    )
    prof = {
        "name": "official", "model": "claude-opus-4-6",
        "max_tokens": 2048, "max_retries": 4, "timeout": 75.0,
        "base_url": "https://example.invalid",
    }
    cli = LLMClient.from_profile(prof, cfg, "sk-test")
    assert cli._cfg.model == "claude-opus-4-6"
    assert cli._cfg.timeout == 75.0
    assert cli._cfg.max_tokens == 2048
    assert cli._cfg.max_retries == 4
    # 工程参数走 yaml 端
    assert cli._cfg.kline_interval == "5m"
