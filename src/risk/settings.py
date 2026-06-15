"""Runtime-adjustable risk settings shared by engine and web."""
from __future__ import annotations

import json
from typing import Any

from src.config.schema import RiskConfig


RUNTIME_RISK_KEY = "risk.effective"
RUNTIME_RISK_VERSION_KEY = "risk.version"

RISK_FIELDS: tuple[str, ...] = (
    "max_leverage",
    "max_order_margin_pct",
    "max_symbol_margin_pct",
    "max_total_margin_pct",
    "max_loss_per_order_margin_pct",
    "max_drawdown_pct",
    "daily_max_loss_pct",
    "liq_distance_min_pct",
    "min_confidence",
)


def risk_public(config: RiskConfig) -> dict[str, Any]:
    return {field: getattr(config, field) for field in RISK_FIELDS}


def validate_risk_payload(payload: dict[str, Any], defaults: RiskConfig) -> RiskConfig:
    """Validate a full or partial runtime payload against the config schema."""
    unknown = set(payload) - set(RISK_FIELDS)
    if unknown:
        raise ValueError(f"unknown runtime risk fields: {','.join(sorted(unknown))}")
    merged = defaults.model_dump()
    merged.update(payload)
    return RiskConfig.model_validate(merged)


def encode_risk(config: RiskConfig) -> str:
    return json.dumps(risk_public(config), sort_keys=True, separators=(",", ":"))


def decode_risk(raw: str | None, defaults: RiskConfig) -> RiskConfig:
    if not raw:
        return defaults.model_copy(deep=True)
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("runtime risk settings must be a JSON object")
    payload.pop("max_loss_per_trade_pct", None)
    return validate_risk_payload(payload, defaults)
