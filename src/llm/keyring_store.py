"""API key 加密存储抽象：keyring 优先，Fernet 兜底，绝不落明文。

设计：
- ``KeyringStore.set(name, plaintext) -> ref`` 返回 "profile://<backend>/<name>"
- ``KeyringStore.get(ref) -> plaintext``
- ``KeyringStore.delete(ref) -> None``
- 后端实现探测顺序：keyring（libsecret/kwallet/...）→ Fernet
- Fernet 模式：主密钥来自环境变量 ``LLM_KEYRING_MASTER_KEY``（bytes / urlsafe-b64）
  若都不可用，set 抛 ``KeyringUnavailable``，调用方需要回退到配置重启模式。
- 模块级单例 + 探测结果缓存：``get_keyring_store()``。

注意：明文永远只驻留在内存，绝不写日志/响应/DB。
"""
from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from typing import Protocol


SERVICE_NAME = "binance-trade-llm"
# 探测到 backend 时写入 runtime_settings 的值。
BACKEND_KEYRING = "keyring"
BACKEND_FERNET = "fernet"
BACKEND_UNAVAILABLE = "unavailable"


class KeyringUnavailable(RuntimeError):
    """系统没有可用的密钥保护后端。"""


class KeyringStore(Protocol):
    backend: str

    def set(self, name: str, plaintext: str) -> str: ...
    def get(self, ref: str) -> str: ...
    def delete(self, ref: str) -> None: ...
    def health(self) -> dict: ...


# ---------- keyring backend ----------
class _KeyringBackend:
    backend = BACKEND_KEYRING

    def __init__(self) -> None:
        import keyring  # type: ignore
        import keyring.errors  # type: ignore

        self._keyring = keyring
        self._err = keyring.errors
        # 触发一次探测：写一个一次性值再删
        try:
            keyring.set_password(SERVICE_NAME, "__probe__", "1")
            keyring.delete_password(SERVICE_NAME, "__probe__")
        except Exception as e:  # noqa: BLE001
            raise KeyringUnavailable(f"keyring backend unusable: {e}") from e

    def set(self, name: str, plaintext: str) -> str:
        if not plaintext:
            raise ValueError("api_key must be non-empty")
        self._keyring.set_password(SERVICE_NAME, name, plaintext)
        return f"profile://{BACKEND_KEYRING}/{name}"

    def get(self, ref: str) -> str:
        name = _ref_to_name(ref)
        v = self._keyring.get_password(SERVICE_NAME, name)
        if v is None:
            raise KeyError(f"key not found in keyring: {ref}")
        return v

    def delete(self, ref: str) -> None:
        name = _ref_to_name(ref)
        try:
            self._keyring.delete_password(SERVICE_NAME, name)
        except self._err.PasswordDeleteError:
            # 已删过，幂等
            pass

    def health(self) -> dict:
        return {"backend": self.backend, "service": SERVICE_NAME}


# ---------- Fernet backend (fallback) ----------
class _FernetBackend:
    backend = BACKEND_FERNET

    # Fernet ciphertext 是 urlsafe-b64；包一层 "fernet:v1:" 前缀便于将来换算法。
    _PREFIX = "fernet:v1:"

    def __init__(self, master_key: bytes) -> None:
        from cryptography.fernet import Fernet, InvalidToken  # type: ignore
        self._InvalidToken = InvalidToken
        # 接受 raw 32 bytes 或 urlsafe-b64
        try:
            if len(master_key) == 44:  # urlsafe-b64 encoded 32 bytes
                key = master_key
            else:
                key = base64.urlsafe_b64encode(hashlib.sha256(master_key).digest())
            self._f = Fernet(key)
        except Exception as e:  # noqa: BLE001
            raise KeyringUnavailable(f"invalid LLM_KEYRING_MASTER_KEY: {e}") from e

    def _wrap(self, ct: bytes) -> str:
        return self._PREFIX + base64.urlsafe_b64encode(ct).decode("ascii")

    def _unwrap(self, s: str) -> bytes:
        return base64.urlsafe_b64decode(s.removeprefix(self._PREFIX).encode("ascii"))

    def set(self, name: str, plaintext: str) -> str:
        if not plaintext:
            raise ValueError("api_key must be non-empty")
        token = self._f.encrypt(plaintext.encode("utf-8"))
        # keyring_ref 现在不再指向 keyring，而是直接装进 llm_profiles.keyring_ref 字段
        # （加密后的密文）。这是设计上的回退：丢失主密钥则全部失效。
        return self._wrap(token)

    def get(self, ref: str) -> str:
        try:
            data = self._unwrap(ref)
            return self._f.decrypt(data).decode("utf-8")
        except self._InvalidToken as e:
            raise KeyError(f"keyring_ref 解密失败（主密钥可能已变更）: {e}") from e

    def delete(self, ref: str) -> None:
        # Fernet 模式无服务端状态，"删除" = 上层把 keyring_ref 清空。
        return None

    def health(self) -> dict:
        return {"backend": self.backend, "service": SERVICE_NAME, "prefix": self._PREFIX}


# ---------- module-level singleton ----------
_cached: KeyringStore | None = None
_cached_status: dict | None = None


def _probe() -> KeyringStore:
    """探测可用 backend：keyring -> Fernet -> 不可用。

    探测失败不会 raise，会缓存一个 ``_Unavailable`` 哨兵，
    调用方 set() 时才报 KeyringUnavailable，便于只读场景继续工作。
    """
    # 1) keyring
    try:
        return _KeyringBackend()
    except Exception:  # noqa: BLE001
        pass
    # 2) Fernet
    master = os.environ.get("LLM_KEYRING_MASTER_KEY")
    if master:
        try:
            key = master.encode("utf-8") if isinstance(master, str) else master
            return _FernetBackend(key)
        except Exception:  # noqa: BLE001
            pass
    return _Unavailable()


class _Unavailable:
    backend = BACKEND_UNAVAILABLE

    def set(self, name: str, plaintext: str) -> str:
        raise KeyringUnavailable(
            "no secure key backend available; install python-keyring + libsecret, "
            "or set LLM_KEYRING_MASTER_KEY env var (urlsfe-b64 or raw bytes)"
        )

    def get(self, ref: str) -> str:
        raise KeyringUnavailable("keyring backend unavailable")

    def delete(self, ref: str) -> None:
        return None

    def health(self) -> dict:
        return {"backend": self.backend, "service": SERVICE_NAME}


def get_keyring_store() -> tuple[KeyringStore, dict]:
    """返回 (单例, 探测状态 dict)。状态 dict 可序列化写 runtime_settings。"""
    global _cached, _cached_status
    if _cached is None:
        _cached = _probe()
        h = _cached.health()
        if _cached.backend == BACKEND_KEYRING:
            _cached_status = {"backend": BACKEND_KEYRING, "available": True}
        elif _cached.backend == BACKEND_FERNET:
            _cached_status = {"backend": BACKEND_FERNET, "available": True}
        else:
            _cached_status = {
                "backend": BACKEND_UNAVAILABLE,
                "available": False,
                "hint": (
                    "install python-keyring + libsecret, "
                    "or set LLM_KEYRING_MASTER_KEY env var"
                ),
            }
    return _cached, _cached_status


def reset_for_test() -> None:
    """测试 hook：清空单例。"""
    global _cached, _cached_status
    _cached = None
    _cached_status = None


def _ref_to_name(ref: str) -> str:
    if not ref.startswith("profile://keyring/"):
        raise ValueError(f"invalid keyring_ref (not keyring backend): {ref}")
    return ref.removeprefix("profile://keyring/")
