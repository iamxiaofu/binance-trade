"""LLM profile 管理（明文持久化 + fallback 链 + 工厂）测试。"""
from __future__ import annotations

import pytest

from src.config.schema import LLMConfig
from src.llm.client import LLMClient
from src.store.repo import Store


async def test_store_llm_profile_crud(tmp_path):
    db = str(tmp_path / "t.db")
    s = Store(db)
    await s.connect()
    try:
        # 初始空
        assert await s.list_llm_profiles() == []
        assert await s.get_active_llm_profile() is None
        # upsert（明文 api_key）
        await s.upsert_llm_profile(
            name="ikuncode", provider="anthropic", model="claude-opus-4-8",
            base_url="https://api.ikuncode.cc", timeout=60.0,
            max_tokens=1024, max_retries=2, api_key="sk-ant-IKUN",
        )
        prof = await s.get_llm_profile("ikuncode")
        assert prof and prof["key_present"] and not prof["is_active"]
        assert prof["api_key_mask"].endswith("IKUN")
        assert "api_key" not in prof  # 对外视图绝不含明文
        assert await s.get_llm_profile_secret("ikuncode") == "sk-ant-IKUN"
        # 重复 upsert：留空 api_key → 保留旧 key，仅改其它字段
        await s.upsert_llm_profile(
            name="ikuncode", provider="anthropic", model="claude-opus-4-6",
            base_url="", timeout=80.0, max_tokens=2048, max_retries=3, api_key="",
        )
        prof2 = await s.get_llm_profile("ikuncode")
        assert prof2["model"] == "claude-opus-4-6"
        assert await s.get_llm_profile_secret("ikuncode") == "sk-ant-IKUN"
        # 互斥激活；激活后 priority 置 0
        await s.upsert_llm_profile(
            name="official", provider="anthropic", model="claude-opus-4-6",
            base_url="", timeout=60.0, max_tokens=1024, max_retries=2,
            api_key="sk-ant-OFFICIAL",
        )
        await s.activate_llm_profile("official")
        assert (await s.get_llm_profile("ikuncode"))["is_active"] is False
        active = await s.get_llm_profile("official")
        assert active["is_active"] is True
        assert active["priority"] == 0
        # 删 active 应抛错
        with pytest.raises(ValueError, match="cannot delete active"):
            await s.delete_llm_profile("official")
        # 删非 active 成功
        assert await s.delete_llm_profile("ikuncode") is True
        rows = await s.list_llm_profiles()
        assert [r["name"] for r in rows] == ["official"]
    finally:
        await s.close()


async def test_enabled_chain_order(tmp_path):
    """get_enabled_llm_profiles 应返回 active 主源 + 备源，按 priority 升序。"""
    db = str(tmp_path / "chain.db")
    s = Store(db)
    await s.connect()
    try:
        await s.upsert_llm_profile(
            name="main", provider="anthropic", model="m1", base_url="", timeout=60,
            max_tokens=1024, max_retries=2, api_key="sk-main",
        )
        await s.upsert_llm_profile(
            name="backup", provider="openai_compatible", model="gpt-x", base_url="https://gw",
            timeout=60, max_tokens=1024, max_retries=1, api_key="sk-backup",
            priority=10, fallback_enabled=True,
        )
        await s.upsert_llm_profile(
            name="ignored", provider="anthropic", model="m2", base_url="", timeout=60,
            max_tokens=1024, max_retries=2, api_key="sk-ig", priority=5,
            fallback_enabled=False,
        )
        await s.activate_llm_profile("main")  # main priority -> 0
        chain = await s.get_enabled_llm_profiles()
        # ignored 既非 active 也未 fallback_enabled → 不在链里
        assert [p["name"] for p in chain] == ["main", "backup"]
    finally:
        await s.close()


def test_llm_client_from_profile_uses_profile_values():
    """from_profile 工厂应该用 profile 里的 provider/model/timeout，而不是 yaml 默认。"""
    cfg = LLMConfig(
        model="claude-opus-4-8", timeout=60, max_tokens=1024,
        max_retries=2, kline_lookback=100,
    )
    prof = {
        "name": "official", "provider": "anthropic", "model": "claude-opus-4-6",
        "max_tokens": 2048, "max_retries": 4, "timeout": 75.0,
        "base_url": "https://example.invalid",
    }
    cli = LLMClient.from_profile(prof, cfg, "sk-test")
    assert cli._cfg.provider == "anthropic"
    assert cli._cfg.model == "claude-opus-4-6"
    assert cli._cfg.timeout == 75.0
    assert cli._cfg.max_tokens == 2048
    assert cli._cfg.max_retries == 4
    # 工程参数走 yaml 端
    assert cli._cfg.kline_interval == "5m"
