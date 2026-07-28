from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import polars as pl

_IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class RecordingValidationReport:
    session_date: str
    status: str
    integrity_passed: bool
    full_session_coverage: bool
    continuity_passed: bool
    acceptance_passed: bool
    manifest_files: int
    manifest_rows: int
    parquet_rows: int
    expected_symbols: tuple[str, ...]
    observed_symbols: tuple[str, ...]
    first_received_ist: str | None
    last_received_ist: str | None
    missing_files: int
    unsafe_manifest_paths: int
    hash_mismatches: int
    duplicate_provider_messages: int
    nonpositive_ltp: int
    crossed_quotes: int
    max_market_gap_seconds: float
    market_gaps_over_limit: int
    quarantine_count: int
    quarantine_reasons: dict[str, int]
    issues: tuple[str, ...]
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_recording_session(
    *,
    project_root: str | Path,
    session_date: str,
    expected_symbols: Iterable[str],
    start_deadline_ist: time,
    end_threshold_ist: time,
    max_gap_seconds_allowed: float = 30.0,
) -> RecordingValidationReport:
    if max_gap_seconds_allowed <= 0:
        raise ValueError("max gap seconds must be positive")
    root = Path(project_root).resolve()
    parsed_date = date.fromisoformat(session_date)
    expected = tuple(sorted(set(expected_symbols)))
    database_path = root / "state" / "stage1.sqlite3"

    manifests, quarantine_reasons = _load_manifests(
        database_path=database_path,
        session_date=session_date,
    )
    issues: list[str] = []
    warnings: list[str] = []
    missing_files = 0
    unsafe_paths = 0
    hash_mismatches = 0
    safe_paths: list[str] = []

    for relative_path, expected_hash, _ in manifests:
        candidate = (root / relative_path).resolve()
        if not candidate.is_relative_to(root):
            unsafe_paths += 1
            continue
        if not candidate.is_file():
            missing_files += 1
            continue
        if _sha256(candidate) != expected_hash:
            hash_mismatches += 1
            continue
        safe_paths.append(str(candidate))

    manifest_rows = sum(row_count for _, _, row_count in manifests)
    market_window_start = datetime.combine(
        parsed_date,
        time(9, 15),
        tzinfo=_IST,
    )
    market_window_end = datetime.combine(
        parsed_date,
        end_threshold_ist,
        tzinfo=_IST,
    )
    parquet_summary = _summarize_parquet(
        safe_paths,
        market_window_start_utc=market_window_start.astimezone(timezone.utc),
        market_window_end_utc=market_window_end.astimezone(timezone.utc),
        max_gap_seconds_allowed=max_gap_seconds_allowed,
    )
    parquet_rows = int(parquet_summary["rows"])
    observed = tuple(sorted(parquet_summary["symbols"]))

    if not manifests:
        issues.append("no raw tick manifests were found for the session")
    if unsafe_paths:
        issues.append(f"{unsafe_paths} manifest paths escaped the project root")
    if missing_files:
        issues.append(f"{missing_files} manifested Parquet files were missing")
    if hash_mismatches:
        issues.append(f"{hash_mismatches} Parquet file hashes did not match manifests")
    if manifest_rows != parquet_rows:
        issues.append(
            f"manifest rows ({manifest_rows}) did not match Parquet rows ({parquet_rows})"
        )
    if observed != expected:
        missing_symbols = sorted(set(expected) - set(observed))
        unexpected_symbols = sorted(set(observed) - set(expected))
        issues.append(
            "symbol coverage differed from the frozen universe "
            f"(missing={missing_symbols}, unexpected={unexpected_symbols})"
        )

    nonpositive_ltp = int(parquet_summary["nonpositive_ltp"])
    crossed_quotes = int(parquet_summary["crossed_quotes"])
    if nonpositive_ltp:
        issues.append(f"{nonpositive_ltp} records had nonpositive LTP")
    if crossed_quotes:
        issues.append(f"{crossed_quotes} records had bid greater than ask")

    max_market_gap = float(parquet_summary["max_market_gap_seconds"])
    gaps_over_limit = int(parquet_summary["market_gaps_over_limit"])
    continuity_passed = gaps_over_limit == 0
    if not continuity_passed:
        warnings.append(
            f"{gaps_over_limit} market-hours feed gaps exceeded "
            f"{max_gap_seconds_allowed:g} seconds (largest={max_market_gap:.3f}s)"
        )

    first_received = _parse_timestamp(parquet_summary["first_received_utc"])
    last_received = _parse_timestamp(parquet_summary["last_received_utc"])
    first_ist = first_received.astimezone(_IST) if first_received else None
    last_ist = last_received.astimezone(_IST) if last_received else None
    start_limit = datetime.combine(parsed_date, start_deadline_ist, tzinfo=_IST)
    end_limit = datetime.combine(parsed_date, end_threshold_ist, tzinfo=_IST)
    full_coverage = bool(
        first_ist is not None
        and last_ist is not None
        and first_ist <= start_limit
        and last_ist >= end_limit
    )
    if not full_coverage and first_ist and last_ist:
        warnings.append(
            "capture did not cover the complete acceptance window "
            f"({start_deadline_ist.isoformat()} start deadline through "
            f"{end_threshold_ist.isoformat()} end threshold IST)"
        )

    duplicate_messages = max(
        0,
        parquet_rows - int(parquet_summary["unique_message_hashes"]),
    )
    if duplicate_messages:
        warnings.append(
            f"{duplicate_messages} repeated provider payloads were retained as raw receipts"
        )
    quarantine_count = sum(quarantine_reasons.values())
    if quarantine_count:
        warnings.append(
            f"{quarantine_count} quarantined messages require recorded review"
        )

    integrity_passed = not issues
    acceptance_passed = integrity_passed and full_coverage and continuity_passed
    if acceptance_passed:
        status = "PASS"
    elif integrity_passed:
        status = "PARTIAL_SESSION"
    else:
        status = "FAIL"

    return RecordingValidationReport(
        session_date=session_date,
        status=status,
        integrity_passed=integrity_passed,
        full_session_coverage=full_coverage,
        continuity_passed=continuity_passed,
        acceptance_passed=acceptance_passed,
        manifest_files=len(manifests),
        manifest_rows=manifest_rows,
        parquet_rows=parquet_rows,
        expected_symbols=expected,
        observed_symbols=observed,
        first_received_ist=first_ist.isoformat() if first_ist else None,
        last_received_ist=last_ist.isoformat() if last_ist else None,
        missing_files=missing_files,
        unsafe_manifest_paths=unsafe_paths,
        hash_mismatches=hash_mismatches,
        duplicate_provider_messages=duplicate_messages,
        nonpositive_ltp=nonpositive_ltp,
        crossed_quotes=crossed_quotes,
        max_market_gap_seconds=max_market_gap,
        market_gaps_over_limit=gaps_over_limit,
        quarantine_count=quarantine_count,
        quarantine_reasons=dict(sorted(quarantine_reasons.items())),
        issues=tuple(issues),
        warnings=tuple(warnings),
    )


def _load_manifests(
    *,
    database_path: Path,
    session_date: str,
) -> tuple[list[tuple[str, str, int]], Counter[str]]:
    if not database_path.is_file():
        return [], Counter()
    with sqlite3.connect(database_path) as connection:
        manifests = connection.execute(
            """
            SELECT relative_path, file_sha256, row_count
            FROM raw_tick_manifests
            WHERE session_date = ?
            ORDER BY relative_path
            """,
            (session_date,),
        ).fetchall()
        quarantine_rows = connection.execute(
            """
            SELECT reason_code, COUNT(*)
            FROM quarantined_market_events
            WHERE relative_path LIKE ?
            GROUP BY reason_code
            """,
            (f"data/raw/quarantine/date={session_date}/%",),
        ).fetchall()
    return (
        [(str(path), str(file_hash), int(rows)) for path, file_hash, rows in manifests],
        Counter({str(reason): int(count) for reason, count in quarantine_rows}),
    )


def _summarize_parquet(
    paths: list[str],
    *,
    market_window_start_utc: datetime,
    market_window_end_utc: datetime,
    max_gap_seconds_allowed: float,
) -> dict[str, Any]:
    if not paths:
        return {
            "rows": 0,
            "unique_message_hashes": 0,
            "symbols": [],
            "first_received_utc": None,
            "last_received_utc": None,
            "nonpositive_ltp": 0,
            "crossed_quotes": 0,
            "max_market_gap_seconds": 0.0,
            "market_gaps_over_limit": 0,
        }
    scan = pl.scan_parquet(paths, hive_partitioning=False)
    row = scan.select(
        pl.len().alias("rows"),
        pl.col("provider_message_hash").n_unique().alias("unique_message_hashes"),
        pl.col("symbol").unique().sort().implode().alias("symbols"),
        pl.col("received_ts").min().alias("first_received_utc"),
        pl.col("received_ts").max().alias("last_received_utc"),
        (pl.col("ltp") <= 0).sum().alias("nonpositive_ltp"),
        (
            pl.col("bid").is_not_null()
            & pl.col("ask").is_not_null()
            & (pl.col("bid") > pl.col("ask"))
        )
        .sum()
        .alias("crossed_quotes"),
    ).collect()
    summary = row.to_dicts()[0]
    timeline = (
        scan.select(
            pl.col("received_ts")
            .str.to_datetime(format="%+", time_zone="UTC")
            .alias("received_at")
        )
        .filter(
            pl.col("received_at").is_between(
                market_window_start_utc,
                market_window_end_utc,
                closed="both",
            )
        )
        .sort("received_at")
        .collect()
    )
    if timeline.height < 2:
        max_gap = 0.0
        gaps_over_limit = 0
    else:
        gaps = timeline["received_at"].diff().dt.total_milliseconds() / 1000
        max_gap = float(gaps.max() or 0.0)
        gaps_over_limit = int((gaps > max_gap_seconds_allowed).sum())
    summary["max_market_gap_seconds"] = max_gap
    summary["market_gaps_over_limit"] = gaps_over_limit
    return summary


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
