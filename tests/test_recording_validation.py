import hashlib
import json
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from stage1.schemas import NormalizedTick, RawTickRecord
from stage1.storage.operational_db import OperationalDatabase
from stage1.storage.parquet_writer import ParquetTickWriter
from stage1.validation.recording import validate_recording_session

_IST = ZoneInfo("Asia/Kolkata")
_SYMBOLS = ("NSE:RELIANCE-EQ", "NSE:TCS-EQ")


def _record(symbol: str, observed_at: datetime) -> RawTickRecord:
    raw = json.dumps(
        {"symbol": symbol, "timestamp": observed_at.isoformat()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return RawTickRecord(
        tick=NormalizedTick(
            symbol=symbol,
            exchange_ts=observed_at,
            received_ts=observed_at,
            ltp=100.0,
            bid=99.0,
            ask=101.0,
            provider_message_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        ),
        raw_message_json=raw,
    )


def _write_session(
    root: Path,
    *,
    start: time,
    end: time,
) -> None:
    session_day = date(2026, 7, 20)
    records = [
        _record(symbol, datetime.combine(session_day, observed, tzinfo=_IST))
        for observed in (start, end)
        for symbol in _SYMBOLS
    ]
    writer = ParquetTickWriter(project_root=root)
    database = OperationalDatabase(root / "state" / "stage1.sqlite3")
    database.initialize()
    for manifest in writer.write_batch(records):
        database.append_raw_tick_manifest(manifest)


def _validate(root: Path, *, max_gap_seconds_allowed: float = 30_000):
    return validate_recording_session(
        project_root=root,
        session_date="2026-07-20",
        expected_symbols=_SYMBOLS,
        start_deadline_ist=time(9, 16),
        end_threshold_ist=time(15, 30),
        max_gap_seconds_allowed=max_gap_seconds_allowed,
    )


def test_complete_intact_session_passes(tmp_path: Path) -> None:
    _write_session(tmp_path, start=time(9, 15), end=time(15, 30))

    report = _validate(tmp_path)

    assert report.status == "PASS"
    assert report.integrity_passed is True
    assert report.full_session_coverage is True
    assert report.continuity_passed is True
    assert report.acceptance_passed is True
    assert report.manifest_rows == report.parquet_rows == 4
    assert report.observed_symbols == tuple(sorted(_SYMBOLS))


def test_intact_short_session_is_reported_as_partial(tmp_path: Path) -> None:
    _write_session(tmp_path, start=time(9, 20), end=time(15, 18))

    report = _validate(tmp_path)

    assert report.status == "PARTIAL_SESSION"
    assert report.integrity_passed is True
    assert report.full_session_coverage is False
    assert report.acceptance_passed is False


def test_large_market_hours_gap_blocks_acceptance(tmp_path: Path) -> None:
    _write_session(tmp_path, start=time(9, 15), end=time(15, 30))

    report = _validate(tmp_path, max_gap_seconds_allowed=30)

    assert report.integrity_passed is True
    assert report.full_session_coverage is True
    assert report.continuity_passed is False
    assert report.market_gaps_over_limit == 1
    assert report.acceptance_passed is False


def test_tampered_parquet_file_fails_integrity(tmp_path: Path) -> None:
    _write_session(tmp_path, start=time(9, 15), end=time(15, 30))
    parquet_file = next((tmp_path / "data" / "raw" / "ticks").rglob("*.parquet"))
    with parquet_file.open("ab") as handle:
        handle.write(b"tampered")

    report = _validate(tmp_path)

    assert report.status == "FAIL"
    assert report.integrity_passed is False
    assert report.hash_mismatches == 1
    assert report.acceptance_passed is False
