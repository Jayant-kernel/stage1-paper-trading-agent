import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from stage1.market.normalizer import normalize_fyers_message, quarantine_fyers_message
from stage1.metrics import RecorderCounters
from stage1.storage.operational_db import OperationalDatabase
from stage1.storage.parquet_writer import ParquetTickWriter, TickSpool

ROOT = Path(__file__).resolve().parents[1]


def _record():
    message = json.loads(
        (ROOT / "tests" / "fixtures" / "fyers_symbol_update.json").read_text(
            encoding="utf-8"
        )
    )
    return normalize_fyers_message(
        message,
        allowed_symbols={"NSE:RELIANCE-EQ"},
        received_at=datetime(2026, 7, 16, 3, 45, 0, 250000, tzinfo=timezone.utc),
    )


def test_parquet_chunk_is_immutable_and_manifested(tmp_path: Path) -> None:
    writer = ParquetTickWriter(project_root=tmp_path)
    database = OperationalDatabase(tmp_path / "state" / "stage1.sqlite3")
    database.initialize()

    first = writer.write_batch([_record()])[0]
    database.append_raw_tick_manifest(first)
    second = writer.write_batch([_record()])[0]
    database.append_raw_tick_manifest(second)

    assert first.manifest_id == second.manifest_id
    assert first.file_sha256 == second.file_sha256
    assert database.count_raw_tick_manifests() == 1
    parquet_files = list((tmp_path / "data" / "raw" / "ticks").rglob("*.parquet"))
    assert len(parquet_files) == 1
    table = pl.read_parquet(parquet_files[0])
    assert table.height == 1
    row = table.to_dicts()[0]
    assert row["exchange_ts"] != row["received_ts"]
    assert row["provider"] == "FYERS"


def test_invalid_message_quarantine_is_redacted_and_append_only(
    tmp_path: Path,
) -> None:
    writer = ParquetTickWriter(project_root=tmp_path)
    database = OperationalDatabase(tmp_path / "state" / "stage1.sqlite3")
    database.initialize()
    event = quarantine_fyers_message(
        {
            "symbol": "NSE:UNKNOWN-EQ",
            "ltp": -1,
            "access_token": "never-write-this-secret",
        },
        received_at=datetime(2026, 7, 16, 3, 45, tzinfo=timezone.utc),
        reason_code="UNKNOWN_SYMBOL",
        reason="symbol is outside configured universe",
    )

    first = writer.write_quarantine(event)
    database.append_quarantine_manifest(first)
    second = writer.write_quarantine(event)
    database.append_quarantine_manifest(second)

    assert first.event_id == second.event_id
    assert database.count_quarantined_market_events() == 1
    quarantine_files = list(
        (tmp_path / "data" / "raw" / "quarantine").rglob("*.json")
    )
    assert len(quarantine_files) == 1
    persisted = quarantine_files[0].read_text(encoding="utf-8")
    assert "never-write-this-secret" not in persisted
    assert "[REDACTED]" in persisted


def test_tick_spool_restart_flushes_without_duplicate_or_overwrite(
    tmp_path: Path,
) -> None:
    database = OperationalDatabase(tmp_path / "state" / "stage1.sqlite3")
    for _ in range(2):
        spool = TickSpool(
            writer=ParquetTickWriter(project_root=tmp_path),
            database=database,
            batch_size=50,
            flush_seconds=0.05,
        )
        spool.submit(_record())
        spool.close()

    assert database.count_raw_tick_manifests() == 1
    assert len(list((tmp_path / "data" / "raw" / "ticks").rglob("*.parquet"))) == 1


def test_tick_spool_counts_records_only_after_successful_flush(
    tmp_path: Path,
) -> None:
    counters = RecorderCounters()
    spool = TickSpool(
        writer=ParquetTickWriter(project_root=tmp_path),
        database=OperationalDatabase(tmp_path / "state" / "stage1.sqlite3"),
        batch_size=50,
        flush_seconds=0.05,
        counters=counters,
    )
    spool.submit(_record())

    assert counters.snapshot().written_parquet_records == 0
    spool.close()
    assert counters.snapshot().written_parquet_records == 1
