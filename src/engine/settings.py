"""Runtime-adjustable engine cadence and LLM throttle settings."""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from src.config.schema import Settings, ThrottleConfig


RUNTIME_ENGINE_KEY = "engine.effective"
RUNTIME_ENGINE_VERSION_KEY = "engine.version"

ENGINE_FIELDS: tuple[str, ...] = (
    "cycle_interval_seconds",
    "price_change_pct",
    "pnl_alert_pct",
    "trigger_on_order_event",
    "max_skip_cycles",
    "feature_snapshot_enabled",
    "ema_spread_cross_min_pct",
    "macd_hist_cross_min_abs",
    "rsi_midline",
    "boll_bandwidth_low_pct",
    "boll_bandwidth_expand_pct",
    "volume_zscore_trigger",
    "micro_return_5m_trigger_pct",
    "micro_range_5m_trigger_pct",
    "near_exit_pnl_pct",
    "review_flat_seconds",
    "review_position_seconds",
    "review_near_exit_seconds",
    "review_high_vol_seconds",
    "close_confirm_min_1m_bars",
    "close_confirm_min_count",
    "close_block_loss_atr_multiple",
    "close_confirm_window_seconds",
    "sltp_adjust_min_seconds",
    "sltp_adjust_min_atr_multiple",
    "breakeven_fee_slippage_buffer_pct",
)


class EngineRuntimeSettings(BaseModel):
    model_config = {"extra": "forbid"}

    cycle_interval_seconds: int = Field(ge=60, le=3600)
    price_change_pct: float = Field(ge=0, le=20)
    pnl_alert_pct: float = Field(ge=0, le=100)
    trigger_on_order_event: bool = True
    max_skip_cycles: int = Field(ge=1, le=100)
    feature_snapshot_enabled: bool = True
    ema_spread_cross_min_pct: float = Field(ge=0, le=5)
    macd_hist_cross_min_abs: float = Field(ge=0, le=1000)
    rsi_midline: float = Field(ge=1, le=99)
    boll_bandwidth_low_pct: float = Field(ge=0, le=100)
    boll_bandwidth_expand_pct: float = Field(ge=0, le=1000)
    volume_zscore_trigger: float = Field(ge=0, le=20)
    micro_return_5m_trigger_pct: float = Field(ge=0, le=100)
    micro_range_5m_trigger_pct: float = Field(ge=0, le=100)
    near_exit_pnl_pct: float = Field(ge=0, le=100)
    review_flat_seconds: int = Field(ge=30, le=86400)
    review_position_seconds: int = Field(ge=30, le=86400)
    review_near_exit_seconds: int = Field(ge=30, le=86400)
    review_high_vol_seconds: int = Field(ge=30, le=86400)
    close_confirm_min_1m_bars: int = Field(default=5, ge=0, le=60)
    close_confirm_min_count: int = Field(default=3, ge=1, le=10)
    close_block_loss_atr_multiple: float = Field(default=1.0, ge=0, le=10)
    close_confirm_window_seconds: int = Field(default=900, ge=60, le=86400)
    sltp_adjust_min_seconds: int = Field(default=900, ge=0, le=86400)
    sltp_adjust_min_atr_multiple: float = Field(default=0.4, ge=0, le=10)
    breakeven_fee_slippage_buffer_pct: float = Field(default=0.15, ge=0, le=5)


def _seconds_from_minutes(value: int) -> int:
    return int(value) * 60


def engine_defaults_from_settings(settings: Settings) -> EngineRuntimeSettings:
    throttle: ThrottleConfig = settings.throttle
    return EngineRuntimeSettings(
        cycle_interval_seconds=settings.cycle.interval_seconds,
        price_change_pct=throttle.price_change_pct,
        pnl_alert_pct=throttle.pnl_alert_pct,
        trigger_on_order_event=throttle.trigger_on_order_event,
        max_skip_cycles=throttle.max_skip_cycles,
        feature_snapshot_enabled=throttle.feature_snapshot_enabled,
        ema_spread_cross_min_pct=throttle.ema_spread_cross_min_pct,
        macd_hist_cross_min_abs=throttle.macd_hist_cross_min_abs,
        rsi_midline=throttle.rsi_midline,
        boll_bandwidth_low_pct=throttle.boll_bandwidth_low_pct,
        boll_bandwidth_expand_pct=throttle.boll_bandwidth_expand_pct,
        volume_zscore_trigger=throttle.volume_zscore_trigger,
        micro_return_5m_trigger_pct=throttle.micro_return_5m_trigger_pct,
        micro_range_5m_trigger_pct=throttle.micro_range_5m_trigger_pct,
        near_exit_pnl_pct=throttle.near_exit_pnl_pct,
        review_flat_seconds=_seconds_from_minutes(throttle.review_flat_minutes),
        review_position_seconds=_seconds_from_minutes(throttle.review_position_minutes),
        review_near_exit_seconds=_seconds_from_minutes(throttle.review_near_exit_minutes),
        review_high_vol_seconds=_seconds_from_minutes(throttle.review_high_vol_minutes),
        close_confirm_min_1m_bars=5,
        close_confirm_min_count=3,
        close_block_loss_atr_multiple=1.0,
        close_confirm_window_seconds=900,
        sltp_adjust_min_seconds=900,
        sltp_adjust_min_atr_multiple=0.4,
        breakeven_fee_slippage_buffer_pct=0.15,
    )


def engine_public(config: EngineRuntimeSettings) -> dict[str, Any]:
    return {field: getattr(config, field) for field in ENGINE_FIELDS}


def validate_engine_payload(
    payload: dict[str, Any],
    defaults: EngineRuntimeSettings,
) -> EngineRuntimeSettings:
    unknown = set(payload) - set(ENGINE_FIELDS)
    if unknown:
        raise ValueError(f"unknown runtime engine fields: {','.join(sorted(unknown))}")
    merged = defaults.model_dump()
    merged.update(payload)
    return EngineRuntimeSettings.model_validate(merged)


def encode_engine(config: EngineRuntimeSettings) -> str:
    return json.dumps(engine_public(config), sort_keys=True, separators=(",", ":"))


def decode_engine(raw: str | None, defaults: EngineRuntimeSettings) -> EngineRuntimeSettings:
    if not raw:
        return defaults.model_copy(deep=True)
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("runtime engine settings must be a JSON object")
    legacy_minutes = {
        "review_flat_minutes": "review_flat_seconds",
        "review_position_minutes": "review_position_seconds",
        "review_near_exit_minutes": "review_near_exit_seconds",
        "review_high_vol_minutes": "review_high_vol_seconds",
    }
    for old_key, new_key in legacy_minutes.items():
        if old_key in payload and new_key not in payload:
            payload[new_key] = int(payload.pop(old_key)) * 60
        else:
            payload.pop(old_key, None)
    return validate_engine_payload(payload, defaults)
