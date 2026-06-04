"""配置加载：读取 config.yaml + .env，校验后返回 (Settings, Credentials)。

用法：
    from src.config.loader import load_config
    settings, creds = load_config()             # 默认 ./config.yaml + ./.env
    settings, creds = load_config("config.yaml")
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from src.config.schema import Credentials, NotifyConfig, Settings


class ConfigError(RuntimeError):
    """配置加载/校验失败。"""


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"config.yaml 解析失败：{e}") from e
    if not isinstance(data, dict):
        raise ConfigError("config.yaml 顶层必须是映射(dict)")
    return data


def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(f"缺少必需的环境变量 {name}（请在 .env 中配置）")
    return val


def _load_credentials(notify: NotifyConfig) -> Credentials:
    creds = Credentials(
        binance_api_key=_require_env("BINANCE_API_KEY"),
        binance_api_secret=_require_env("BINANCE_API_SECRET"),
        anthropic_api_key=_require_env("ANTHROPIC_API_KEY"),
    )
    # 仅在开启 Telegram 时才强制要求其密钥
    if notify.telegram_enabled:
        creds.telegram_bot_token = _require_env(notify.telegram_bot_token_env)
        creds.telegram_chat_id = _require_env(notify.telegram_chat_id_env)
    return creds


def load_config(
    config_path: str | Path = "config.yaml",
    env_path: str | Path | None = ".env",
) -> tuple[Settings, Credentials]:
    """加载并校验配置。任何问题都抛出 ConfigError（含清晰原因）。"""
    if env_path is not None and Path(env_path).exists():
        load_dotenv(env_path, override=False)

    raw = _load_yaml(Path(config_path))

    try:
        settings = Settings.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"配置校验失败：\n{e}") from e

    creds = _load_credentials(settings.notify)

    # mainnet + 非 dry_run 是真实下单，给一个显式提示位（调用方决定是否二次确认）
    return settings, creds


# 进程级单例缓存
_cached: tuple[Settings, Credentials] | None = None


def get_settings(reload: bool = False, **kwargs) -> tuple[Settings, Credentials]:
    """返回缓存的配置单例；reload=True 强制重读。"""
    global _cached
    if _cached is None or reload:
        _cached = load_config(**kwargs)
    return _cached
