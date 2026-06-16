from __future__ import annotations

import pytest

from src.engine.settings import (
    decode_engine,
    encode_engine,
    engine_defaults_from_settings,
    validate_engine_payload,
)


def test_engine_defaults_convert_review_minutes_to_seconds(settings):
    defaults = engine_defaults_from_settings(settings)

    assert defaults.cycle_interval_seconds == settings.cycle.interval_seconds
    assert defaults.review_flat_seconds == settings.throttle.review_flat_minutes * 60
    assert defaults.review_position_seconds == settings.throttle.review_position_minutes * 60
    assert defaults.review_near_exit_seconds == settings.throttle.review_near_exit_minutes * 60
    assert defaults.review_high_vol_seconds == settings.throttle.review_high_vol_minutes * 60


def test_engine_payload_accepts_90_second_cycle(settings):
    defaults = engine_defaults_from_settings(settings)

    updated = validate_engine_payload({
        "cycle_interval_seconds": 90,
        "review_position_seconds": 90,
    }, defaults)

    assert updated.cycle_interval_seconds == 90
    assert updated.review_position_seconds == 90


def test_engine_payload_rejects_unknown_field(settings):
    defaults = engine_defaults_from_settings(settings)

    with pytest.raises(ValueError, match="unknown runtime engine fields"):
        validate_engine_payload({"unexpected": 1}, defaults)


def test_engine_decode_migrates_legacy_review_minutes(settings):
    defaults = engine_defaults_from_settings(settings)
    raw = encode_engine(defaults)
    raw = raw.replace('"review_flat_seconds":3600', '"review_flat_minutes":2')

    decoded = decode_engine(raw, defaults)

    assert decoded.review_flat_seconds == 120
