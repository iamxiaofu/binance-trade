"""Runtime-adjustable execution and maker-order settings."""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator

from src.config.schema import ExecutionConfig, ExecutionMode, MakerUnfilledAction, Settings


RUNTIME_EXECUTION_KEY = "execution.effective"
RUNTIME_EXECUTION_VERSION_KEY = "execution.version"

EXECUTION_FIELDS: tuple[str, ...] = (
    "entry_mode",
    "maker_timeout_seconds",
    "maker_poll_seconds",
    "maker_max_requotes",
    "maker_price_offset_bps",
    "maker_unfilled_action",
    "market_slippage_bps",
    "market_slippage_bps_per_symbol",
    "rate_limit_backoff",
    "max_order_retries",
)

EXECUTION_FIXED_FIELDS: tuple[str, ...] = (
    "maker_time_in_force",
    "normal_exit_mode",
    "emergency_exit_mode",
    "partial_fill_action",
    "attach_sl_tp",
    "recv_window",
    "order_type",
)


class ExecutionRuntimeSettings(BaseModel):
    model_config = {"extra": "forbid", "use_enum_values": True}

    entry_mode: ExecutionMode
    maker_timeout_seconds: float = Field(gt=0, le=120)
    maker_poll_seconds: float = Field(gt=0, le=10)
    maker_max_requotes: int = Field(ge=0, le=10)
    maker_price_offset_bps: float = Field(ge=0, le=100)
    maker_unfilled_action: MakerUnfilledAction
    market_slippage_bps: float = Field(gt=0, le=100)
    market_slippage_bps_per_symbol: dict[str, float] = Field(default_factory=dict)
    rate_limit_backoff: float = Field(gt=1.0, le=10.0)
    max_order_retries: int = Field(ge=0, le=10)

    @field_validator("market_slippage_bps_per_symbol")
    @classmethod
    def _normalize_per_symbol(cls, value: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for raw_symbol, raw_limit in (value or {}).items():
            symbol = str(raw_symbol or "").upper().strip()
            if not symbol:
                raise ValueError("market_slippage_bps_per_symbol contains empty symbol")
            limit = float(raw_limit)
            if limit <= 0 or limit > 100:
                raise ValueError(f"{symbol} market slippage bps must be >0 and <=100")
            out[symbol] = limit
        return out


def execution_defaults_from_settings(settings: Settings) -> ExecutionRuntimeSettings:
    return ExecutionRuntimeSettings(
        entry_mode=settings.execution.entry_mode or ExecutionMode.MARKET_TAKER,
        maker_timeout_seconds=settings.execution.maker_timeout_seconds,
        maker_poll_seconds=settings.execution.maker_poll_seconds,
        maker_max_requotes=settings.execution.maker_max_requotes,
        maker_price_offset_bps=settings.execution.maker_price_offset_bps,
        maker_unfilled_action=settings.execution.maker_unfilled_action,
        market_slippage_bps=settings.execution.market_slippage_bps,
        market_slippage_bps_per_symbol=settings.execution.market_slippage_bps_per_symbol,
        rate_limit_backoff=settings.execution.rate_limit_backoff,
        max_order_retries=settings.execution.max_order_retries,
    )


def execution_public(config: ExecutionRuntimeSettings | ExecutionConfig) -> dict[str, Any]:
    return {field: _json_value(getattr(config, field)) for field in EXECUTION_FIELDS}


def execution_fixed_public(config: ExecutionConfig) -> dict[str, Any]:
    return {field: _json_value(getattr(config, field)) for field in EXECUTION_FIXED_FIELDS}


def execution_runtime_to_config(
    runtime: ExecutionRuntimeSettings,
    base: ExecutionConfig,
) -> ExecutionConfig:
    payload = base.model_dump(mode="python")
    payload.update(runtime.model_dump(mode="python"))
    return ExecutionConfig.model_validate(payload)


def validate_execution_payload(
    payload: dict[str, Any],
    defaults: ExecutionRuntimeSettings,
    *,
    allowed_symbols: set[str] | None = None,
) -> ExecutionRuntimeSettings:
    unknown = set(payload) - set(EXECUTION_FIELDS)
    if unknown:
        raise ValueError(f"unknown runtime execution fields: {','.join(sorted(unknown))}")
    merged = defaults.model_dump(mode="python")
    merged.update(payload)
    validated = ExecutionRuntimeSettings.model_validate(merged)
    if allowed_symbols is not None:
        unknown_symbols = set(validated.market_slippage_bps_per_symbol) - allowed_symbols
        if unknown_symbols:
            raise ValueError(
                "unknown market_slippage_bps_per_symbol symbols: "
                + ",".join(sorted(unknown_symbols))
            )
    return validated


def encode_execution(config: ExecutionRuntimeSettings) -> str:
    return json.dumps(execution_public(config), sort_keys=True, separators=(",", ":"))


def decode_execution(
    raw: str | None,
    defaults: ExecutionRuntimeSettings,
    *,
    allowed_symbols: set[str] | None = None,
) -> ExecutionRuntimeSettings:
    if not raw:
        return defaults.model_copy(deep=True)
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("runtime execution settings must be a JSON object")
    return validate_execution_payload(payload, defaults, allowed_symbols=allowed_symbols)


def _json_value(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    return value
