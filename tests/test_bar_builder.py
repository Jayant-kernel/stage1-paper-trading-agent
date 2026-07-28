import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from stage1.market.bar_builder import build_minute_bars, write_bar_artifact


def _tick(
    *,
    symbol: str,
    exchange_at: datetime,
    received_at: datetime,
    ltp: float,
    cumulative_volume: float,
    message_id: str,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "exchange_ts": exchange_at.isoformat(),
        "received_ts": received_at.isoformat(),
        "ltp": ltp,
        "bid": ltp - 0.05,
        "ask": ltp + 0.05,
        "cumulative_volume": cumulative_volume,
        "provider_message_hash": hashlib.sha256(message_id.encode()).hexdigest(),
    }


def test_bar_builder_deduplicates_sorts_flags_and_computes_volume() -> None:
    base = datetime(2026, 7, 20, 3, 45, tzinfo=timezone.utc)
    ticks = [
        _tick(
            symbol="NSE:RELIANCE-EQ",
            exchange_at=base + timedelta(seconds=30),
            received_at=base + timedelta(seconds=31),
            ltp=100,
            cumulative_volume=100,
            message_id="later-exchange",
        ),
        _tick(
            symbol="NSE:RELIANCE-EQ",
            exchange_at=base + timedelta(seconds=30),
            received_at=base + timedelta(seconds=32),
            ltp=100,
            cumulative_volume=100,
            message_id="later-exchange",
        ),
        _tick(
            symbol="NSE:RELIANCE-EQ",
            exchange_at=base + timedelta(seconds=20),
            received_at=base + timedelta(seconds=40),
            ltp=99,
            cumulative_volume=90,
            message_id="out-of-order",
        ),
        _tick(
            symbol="NSE:RELIANCE-EQ",
            exchange_at=base + timedelta(minutes=2, seconds=5),
            received_at=base + timedelta(minutes=2, seconds=6),
            ltp=101,
            cumulative_volume=150,
            message_id="next-bar",
        ),
    ]

    result = build_minute_bars(pl.DataFrame(ticks), stale_tick_seconds=5)

    assert result.raw_tick_count == 4
    assert result.deduplicated_tick_count == 3
    assert result.duplicate_receipts_dropped == 1
    assert result.out_of_order_tick_count == 1
    assert result.stale_tick_count == 1
    assert result.missing_minutes == 1
    assert result.rejected_bars == ()
    rows = result.frame.sort("bar_start").to_dicts()
    assert len(rows) == 2
    assert rows[0]["open"] == 99
    assert rows[0]["high"] == 100
    assert rows[0]["low"] == 99
    assert rows[0]["close"] == 100
    assert rows[0]["volume"] is None
    assert rows[1]["volume"] == 50
    assert rows[1]["missing_minutes_before"] == 1
    assert "DUPLICATE_RECEIPT_DROPPED" in rows[0]["quality_flags"]
    assert "OUT_OF_ORDER_TICK" in rows[0]["quality_flags"]
    assert "STALE_TICK" in rows[0]["quality_flags"]
    assert "MISSING_PREVIOUS_MINUTE" in rows[1]["quality_flags"]


def test_bar_artifact_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    base = datetime(2026, 7, 20, 3, 45, tzinfo=timezone.utc)
    result = build_minute_bars(
        pl.DataFrame(
            [
                _tick(
                    symbol="NSE:TCS-EQ",
                    exchange_at=base,
                    received_at=base + timedelta(milliseconds=100),
                    ltp=3200,
                    cumulative_volume=10,
                    message_id="one",
                )
            ]
        ),
        stale_tick_seconds=5,
    )

    first = write_bar_artifact(
        project_root=tmp_path,
        session_date="2026-07-20",
        result=result,
    )
    second = write_bar_artifact(
        project_root=tmp_path,
        session_date="2026-07-20",
        result=result,
    )

    assert first == second
    assert first.row_count == 1
    assert len(list((tmp_path / "data" / "derived" / "bars").rglob("*.parquet"))) == 1


def test_bar_builder_rejects_missing_required_columns() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        build_minute_bars(
            pl.DataFrame({"symbol": ["NSE:TCS-EQ"]}),
            stale_tick_seconds=5,
        )
