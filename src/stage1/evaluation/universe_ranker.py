from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import polars as pl


_REQUIRED = {"symbol", "resolution", "bar_start", "session_date", "open", "high", "low", "close", "volume"}


@dataclass(frozen=True)
class PreparedHistory:
    frame: pl.DataFrame
    ranking: pl.DataFrame
    selected_symbols: tuple[str, ...]
    split_dates: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class PreparedHistoryArtifact:
    dataset_relative_path: str
    dataset_sha256: str
    ranking_relative_path: str
    ranking_sha256: str
    report_relative_path: str
    report_sha256: str


def prepare_walk_forward_history(
    bars: pl.DataFrame,
    *,
    trade_symbols: Iterable[str],
    selected_count: int = 4,
    embargo_sessions: int = 1,
) -> PreparedHistory:
    missing = sorted(_REQUIRED - set(bars.columns))
    if missing:
        raise ValueError(f"historical bars are missing columns: {missing}")
    if selected_count <= 0:
        raise ValueError("selected symbol count must be positive")
    symbols = tuple(dict.fromkeys(trade_symbols))
    if not symbols:
        raise ValueError("at least one trade symbol is required")

    frame = (
        bars.filter(pl.col("symbol").is_in(list(symbols)))
        .unique(subset=["symbol", "resolution", "bar_start"], keep="first")
        .sort(["session_date", "bar_start", "symbol"])
    )
    dates = frame["session_date"].unique().sort().to_list()
    if len(dates) < 15:
        raise ValueError("at least 15 historical sessions are required for chronological splits")

    train_end = max(1, int(len(dates) * 0.60))
    validation_end = max(train_end + embargo_sessions + 1, int(len(dates) * 0.80))
    test_start = validation_end + embargo_sessions
    if test_start >= len(dates):
        raise ValueError("not enough sessions remain after walk-forward embargoes")
    train_dates = tuple(str(item) for item in dates[:train_end])
    validation_dates = tuple(str(item) for item in dates[train_end + embargo_sessions : validation_end])
    test_dates = tuple(str(item) for item in dates[test_start:])
    selected_dates = set(train_dates + validation_dates + test_dates)
    embargo_dates = tuple(str(item) for item in dates if str(item) not in selected_dates)
    split_map = {item: "TRAIN" for item in train_dates}
    split_map.update({item: "VALIDATION" for item in validation_dates})
    split_map.update({item: "TEST" for item in test_dates})
    split_map.update({item: "EMBARGO" for item in embargo_dates})
    frame = frame.with_columns(
        pl.col("session_date")
        .cast(pl.String)
        .replace_strict(split_map)
        .alias("dataset_split")
    )

    train = frame.filter(pl.col("dataset_split") == "TRAIN")
    metrics = [_symbol_metrics(train.filter(pl.col("symbol") == symbol), symbol) for symbol in symbols]
    _add_normalized_metric(metrics, "median_log_turnover", "liquidity_score", higher_is_better=True)
    _add_normalized_metric(metrics, "volatility_dispersion", "stability_score", higher_is_better=False)
    for row in metrics:
        row["research_suitability_score"] = round(
            100.0
            * (
                0.35 * float(row["coverage_score"])
                + 0.30 * float(row["liquidity_score"])
                + 0.20 * float(row["stability_score"])
                + 0.15 * float(row["non_jump_score"])
            ),
            6,
        )
        row["eligible"] = bool(
            row["session_count"] >= min(20, len(train_dates))
            and row["coverage_score"] >= 0.85
            and row["zero_volume_fraction"] <= 0.10
        )
        row["selection_basis"] = "TRAIN_ONLY_DATA_QUALITY_LIQUIDITY_STABILITY"

    ordered = sorted(
        metrics,
        key=lambda row: (
            not bool(row["eligible"]),
            -float(row["research_suitability_score"]),
            str(row["symbol"]),
        ),
    )
    selected = tuple(str(row["symbol"]) for row in ordered if row["eligible"])[
        : min(selected_count, len(symbols))
    ]
    if not selected:
        raise ValueError("no symbol met the minimum historical data-quality gate")
    for rank, row in enumerate(ordered, start=1):
        row["rank"] = rank
        row["selected_for_simple_baseline"] = row["symbol"] in selected
    ranking = pl.DataFrame(ordered).sort("rank")
    return PreparedHistory(
        frame=frame,
        ranking=ranking,
        selected_symbols=selected,
        split_dates={
            "TRAIN": train_dates,
            "VALIDATION": validation_dates,
            "TEST": test_dates,
            "EMBARGO": embargo_dates,
        },
    )


def write_prepared_history(
    *,
    project_root: str | Path,
    start: str,
    end: str,
    resolution: str,
    prepared: PreparedHistory,
) -> PreparedHistoryArtifact:
    root = Path(project_root).resolve()
    destination_root = root / "data" / "historical" / "prepared"
    destination_root.mkdir(parents=True, exist_ok=True)
    prefix = f"{start}_{end}_r{resolution}"
    dataset_path, dataset_hash = _write_parquet(
        destination_root, f"walk-forward-{prefix}", prepared.frame
    )
    ranking_path, ranking_hash = _write_parquet(
        destination_root, f"universe-ranking-{prefix}", prepared.ranking
    )
    report = {
        "schema_version": "historical_research_report_v1",
        "period": {"start": start, "end": end, "resolution": resolution},
        "selection_basis": "TRAIN_ONLY_DATA_QUALITY_LIQUIDITY_STABILITY",
        "selected_symbols": list(prepared.selected_symbols),
        "warning": "Research suitability is not a promise of predictability or profit.",
        "split_dates": {key: list(value) for key, value in prepared.split_dates.items()},
        "dataset": {
            "relative_path": dataset_path.relative_to(root).as_posix(),
            "sha256": dataset_hash,
            "rows": prepared.frame.height,
        },
        "ranking": {
            "relative_path": ranking_path.relative_to(root).as_posix(),
            "sha256": ranking_hash,
            "rows": prepared.ranking.height,
        },
    }
    report_bytes = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode("utf-8")
    report_hash = hashlib.sha256(report_bytes).hexdigest()
    report_path = destination_root / f"research-report-{prefix}-{report_hash[:16]}.json"
    _write_immutable(report_path, report_bytes)
    return PreparedHistoryArtifact(
        dataset_relative_path=dataset_path.relative_to(root).as_posix(),
        dataset_sha256=dataset_hash,
        ranking_relative_path=ranking_path.relative_to(root).as_posix(),
        ranking_sha256=ranking_hash,
        report_relative_path=report_path.relative_to(root).as_posix(),
        report_sha256=report_hash,
    )


def _symbol_metrics(frame: pl.DataFrame, symbol: str) -> dict[str, Any]:
    if frame.is_empty():
        return {
            "symbol": symbol,
            "session_count": 0,
            "bar_count": 0,
            "coverage_score": 0.0,
            "zero_volume_fraction": 1.0,
            "median_log_turnover": 0.0,
            "volatility_dispersion": 1.0,
            "non_jump_score": 0.0,
        }
    resolution = str(frame["resolution"][0])
    expected = _expected_bars_per_session(resolution)
    daily_counts = frame.group_by("session_date").len()["len"].to_list()
    coverage = min(1.0, statistics.median(float(value) for value in daily_counts) / expected)
    zero_volume = frame.filter(pl.col("volume") <= 0).height / frame.height
    turnovers = [
        max(0.0, float(row["close"]) * float(row["volume"]))
        for row in frame.select("close", "volume").to_dicts()
    ]
    positive_turnover = [math.log1p(value) for value in turnovers if value > 0]
    median_turnover = statistics.median(positive_turnover) if positive_turnover else 0.0

    daily_volatility: list[float] = []
    jumps = 0
    return_count = 0
    for day in frame.partition_by("session_date"):
        closes = [float(value) for value in day.sort("bar_start")["close"].to_list()]
        returns = [(current / previous) - 1.0 for previous, current in zip(closes, closes[1:]) if previous > 0]
        if returns:
            daily_volatility.append(math.sqrt(sum(value * value for value in returns)))
            jumps += sum(abs(value) >= 0.01 for value in returns)
            return_count += len(returns)
    if daily_volatility and statistics.median(daily_volatility) > 0:
        centre = statistics.median(daily_volatility)
        dispersion = statistics.median(abs(value - centre) for value in daily_volatility) / centre
    else:
        dispersion = 1.0
    non_jump_score = max(0.0, 1.0 - (jumps / return_count) * 20.0) if return_count else 0.0
    return {
        "symbol": symbol,
        "session_count": len(daily_counts),
        "bar_count": frame.height,
        "coverage_score": round(coverage, 8),
        "zero_volume_fraction": round(zero_volume, 8),
        "median_log_turnover": round(median_turnover, 8),
        "volatility_dispersion": round(dispersion, 8),
        "non_jump_score": round(non_jump_score, 8),
    }


def _expected_bars_per_session(resolution: str) -> float:
    if resolution == "D":
        return 1.0
    minutes = int(resolution)
    return math.ceil(375 / minutes)


def _add_normalized_metric(
    rows: list[dict[str, Any]], source: str, destination: str, *, higher_is_better: bool
) -> None:
    values = [float(row[source]) for row in rows]
    minimum, maximum = min(values), max(values)
    for row in rows:
        if maximum == minimum:
            score = 1.0
        else:
            score = (float(row[source]) - minimum) / (maximum - minimum)
        row[destination] = round(score if higher_is_better else 1.0 - score, 8)


def _write_parquet(root: Path, prefix: str, frame: pl.DataFrame) -> tuple[Path, str]:
    temporary = root / f".tmp-{uuid.uuid4().hex}.parquet"
    frame.write_parquet(temporary, compression="zstd", statistics=True)
    digest = _sha256(temporary)
    destination = root / f"{prefix}-{digest[:16]}.parquet"
    if destination.exists():
        temporary.unlink(missing_ok=True)
    else:
        os.replace(temporary, destination)
    return destination, digest


def _write_immutable(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError("refusing to replace an immutable research report")
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
