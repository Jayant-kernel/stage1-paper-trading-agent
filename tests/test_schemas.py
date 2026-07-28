from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from stage1.schemas import ModelPolicy, NormalizedTick

HASH = "a" * 64


def test_normalized_tick_preserves_both_timestamps_in_utc() -> None:
    exchange_ts = datetime(2026, 7, 16, 3, 45, tzinfo=timezone.utc)
    received_ts = datetime(2026, 7, 16, 3, 45, 0, 250000, tzinfo=timezone.utc)
    tick = NormalizedTick(
        symbol="NSE:TCS-EQ",
        exchange_ts=exchange_ts,
        received_ts=received_ts,
        ltp=3300.0,
        provider_message_hash=HASH,
    )
    assert tick.exchange_ts == exchange_ts
    assert tick.received_ts == received_ts
    assert tick.exchange_ts is not tick.received_ts


def test_naive_tick_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError):
        NormalizedTick(
            symbol="NSE:TCS-EQ",
            exchange_ts=datetime(2026, 7, 16, 9, 15),
            received_ts=datetime.now(timezone.utc),
            ltp=3300.0,
            provider_message_hash=HASH,
        )


def test_crossed_quote_is_rejected() -> None:
    with pytest.raises(ValidationError):
        NormalizedTick(
            symbol="NSE:TCS-EQ",
            exchange_ts=datetime.now(timezone.utc),
            received_ts=datetime.now(timezone.utc),
            ltp=3300.0,
            bid=3301.0,
            ask=3300.0,
            provider_message_hash=HASH,
        )


def test_hold_policy_must_be_flat() -> None:
    with pytest.raises(ValidationError):
        ModelPolicy(
            symbol="NSE:TCS-EQ",
            action="HOLD",
            exposure="HALF",
            evidence_ids=[],
            risk_flags=[],
            invalidation_conditions=[],
            confidence_rank=0,
            valid_until=datetime.now(timezone.utc),
        )

