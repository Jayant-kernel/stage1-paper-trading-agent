import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stage1.market.normalizer import (
    MarketMessageError,
    UnknownSymbolError,
    UnsupportedMarketMessage,
    normalize_fyers_message,
)

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = frozenset({"NSE:RELIANCE-EQ", "NSE:TCS-EQ"})


def _fixture() -> dict[str, object]:
    return json.loads(
        (ROOT / "tests" / "fixtures" / "fyers_symbol_update.json").read_text(
            encoding="utf-8"
        )
    )


def _control_fixture(name: str) -> dict[str, object]:
    return json.loads(
        (ROOT / "tests" / "fixtures" / name).read_text(encoding="utf-8")
    )


def _named_fixture(name: str) -> dict[str, object]:
    return json.loads(
        (ROOT / "tests" / "fixtures" / name).read_text(encoding="utf-8")
    )


def test_recorded_message_normalizes_deterministically() -> None:
    message = _fixture()
    received_at = datetime(2026, 7, 16, 3, 45, 0, 123000, tzinfo=timezone.utc)
    first = normalize_fyers_message(
        message,
        allowed_symbols=ALLOWED,
        received_at=received_at,
    )
    second = normalize_fyers_message(
        dict(reversed(list(message.items()))),
        allowed_symbols=ALLOWED,
        received_at=received_at,
    )
    assert first == second
    assert first.tick.symbol == "NSE:RELIANCE-EQ"
    assert first.tick.exchange_ts.tzinfo is not None
    assert first.tick.received_ts == received_at
    assert first.tick.provider_message_hash == second.tick.provider_message_hash
    assert first.tick.bid == 1512.3
    assert first.tick.ask == 1512.4
    assert first.tick.cumulative_volume == 482910


def test_receive_and_exchange_timestamps_are_independent() -> None:
    message = _fixture()
    received_at = datetime.fromtimestamp(
        int(message["exch_feed_time"]),
        tz=timezone.utc,
    ) + timedelta(milliseconds=321)
    record = normalize_fyers_message(
        message,
        allowed_symbols=ALLOWED,
        received_at=received_at,
    )
    assert record.tick.received_ts - record.tick.exchange_ts == timedelta(
        milliseconds=321
    )


def test_unknown_symbol_is_quarantined_at_adapter_boundary() -> None:
    message = _fixture()
    message["symbol"] = "NSE:NOT-IN-UNIVERSE-EQ"
    with pytest.raises(UnknownSymbolError):
        normalize_fyers_message(message, allowed_symbols=ALLOWED)


@pytest.mark.parametrize(
    "fixture_name",
    (
        "fyers_control_authentication_done.json",
        "fyers_control_full_mode_on.json",
    ),
)
def test_exact_fyers_control_messages_are_not_mistaken_for_ticks(
    fixture_name: str,
) -> None:
    with pytest.raises(UnsupportedMarketMessage):
        normalize_fyers_message(
            _control_fixture(fixture_name),
            allowed_symbols=ALLOWED,
        )


def test_status_field_is_never_used_as_a_symbol() -> None:
    with pytest.raises(MarketMessageError, match=r"\(symbol\)"):
        normalize_fyers_message(
            {"s": "NSE:RELIANCE-EQ", "ltp": 100, "exch_feed_time": 1_700_000_000},
            allowed_symbols=ALLOWED,
        )


def test_missing_exchange_timestamp_is_rejected_not_synthesized() -> None:
    message = _fixture()
    del message["exch_feed_time"]
    with pytest.raises(MarketMessageError, match="missing required"):
        normalize_fyers_message(message, allowed_symbols=ALLOWED)


def test_impossible_price_is_wrapped_as_rejected_market_message() -> None:
    message = _fixture()
    message["ltp"] = -1
    with pytest.raises(MarketMessageError, match="normalized schema"):
        normalize_fyers_message(message, allowed_symbols=ALLOWED)


def test_sensitive_provider_fields_are_redacted_before_persistence() -> None:
    message = _fixture()
    message["access_token"] = "must-not-reach-parquet"
    record = normalize_fyers_message(message, allowed_symbols=ALLOWED)
    assert "must-not-reach-parquet" not in record.raw_message_json
    assert "[REDACTED]" in record.raw_message_json


def test_after_hours_zero_price_and_quantity_mean_unavailable_quote_side() -> None:
    message = _named_fixture("fyers_after_hours_zero_quotes.json")

    record = normalize_fyers_message(message, allowed_symbols=ALLOWED)

    assert record.tick.bid is None
    assert record.tick.bid_qty is None
    assert record.tick.ask is None
    assert record.tick.ask_qty is None
    persisted_raw = json.loads(record.raw_message_json)
    assert persisted_raw["bid_price"] == 0
    assert persisted_raw["bid_size"] == 0
    assert persisted_raw["ask_price"] == 0
    assert persisted_raw["ask_size"] == 0


@pytest.mark.parametrize(
    ("price_field", "quantity_field", "side"),
    (
        ("bid_price", "bid_size", "bid"),
        ("ask_price", "ask_size", "ask"),
    ),
)
def test_zero_price_with_positive_quantity_is_rejected(
    price_field: str,
    quantity_field: str,
    side: str,
) -> None:
    message = _fixture()
    message[price_field] = 0
    message[quantity_field] = 25

    with pytest.raises(
        MarketMessageError,
        match=rf"{side} price is zero without a matching zero quantity",
    ):
        normalize_fyers_message(message, allowed_symbols=ALLOWED)
