from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import polars as pl
from pydantic import ValidationError

from stage1.schemas import MinuteBar

_REQUIRED_COLUMNS = {
    "symbol",
    "exchange_ts",
    "received_ts",
    "ltp",
    "bid",
    "ask",
    "cumulative_volume",
    "provider_message_hash",
}


@dataclass(frozen=True)
class RejectedBar:
    symbol: str
    bar_start: str
    reason: str


@dataclass(frozen=True)
class BarBuildResult:
    frame: pl.DataFrame
    raw_tick_count: int
    deduplicated_tick_count: int
    duplicate_receipts_dropped: int
    out_of_order_tick_count: int
    stale_tick_count: int
    clock_skew_tick_count: int
    missing_minutes: int
    rejected_bars: tuple[RejectedBar, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "raw_tick_count": self.raw_tick_count,
            "deduplicated_tick_count": self.deduplicated_tick_count,
            "minute_bar_count": self.frame.height,
            "duplicate_receipts_dropped": self.duplicate_receipts_dropped,
            "out_of_order_tick_count": self.out_of_order_tick_count,
            "stale_tick_count": self.stale_tick_count,
            "clock_skew_tick_count": self.clock_skew_tick_count,
            "missing_minutes": self.missing_minutes,
            "rejected_bar_count": len(self.rejected_bars),
            "rejected_bars": [asdict(item) for item in self.rejected_bars],
        }


@dataclass(frozen=True)
class BarArtifact:
    relative_path: str
    file_sha256: str
    row_count: int


def build_minute_bars(
    ticks: pl.DataFrame,
    *,
    stale_tick_seconds: float,
) -> BarBuildResult:
    if stale_tick_seconds <= 0:
        raise ValueError("stale tick seconds must be positive")
    missing_columns = sorted(_REQUIRED_COLUMNS - set(ticks.columns))
    if missing_columns:
        raise ValueError(f"raw tick frame is missing columns: {missing_columns}")
    if ticks.is_empty():
        return BarBuildResult(
            frame=pl.DataFrame(),
            raw_tick_count=0,
            deduplicated_tick_count=0,
            duplicate_receipts_dropped=0,
            out_of_order_tick_count=0,
            stale_tick_count=0,
            clock_skew_tick_count=0,
            missing_minutes=0,
            rejected_bars=(),
        )

    ordered_by_receipt = (
        ticks.with_row_index("_source_index")
        .with_columns(
            pl.col("exchange_ts")
            .str.to_datetime(format="%+", time_zone="UTC")
            .alias("_exchange_at"),
            pl.col("received_ts")
            .str.to_datetime(format="%+", time_zone="UTC")
            .alias("_received_at"),
        )
        .sort(["symbol", "_received_at", "_source_index"])
        .with_columns(
            (
                pl.col("_exchange_at")
                < pl.col("_exchange_at").shift(1).over("symbol")
            )
            .fill_null(False)
            .alias("_out_of_order"),
            (pl.col("_received_at") < pl.col("_exchange_at"))
            .fill_null(False)
            .alias("_clock_skew"),
            (
                (pl.col("_received_at") - pl.col("_exchange_at"))
                .dt.total_milliseconds()
                > stale_tick_seconds * 1000
            )
            .fill_null(False)
            .alias("_stale"),
            pl.len().over("provider_message_hash").alias("_hash_receipts"),
        )
    )
    deduplicated = (
        ordered_by_receipt.unique(
            subset=["provider_message_hash"],
            keep="first",
            maintain_order=True,
        )
        .with_columns(
            (pl.col("_hash_receipts") - 1).alias("_duplicates_dropped"),
            pl.col("_exchange_at").dt.truncate("1m").alias("_bar_start"),
        )
        .sort(["symbol", "_exchange_at", "_received_at", "_source_index"])
    )
    grouped = (
        deduplicated.group_by(["symbol", "_bar_start"], maintain_order=True)
        .agg(
            pl.col("ltp").first().alias("open"),
            pl.col("ltp").max().alias("high"),
            pl.col("ltp").min().alias("low"),
            pl.col("ltp").last().alias("close"),
            pl.col("bid").last().alias("bid_close"),
            pl.col("ask").last().alias("ask_close"),
            pl.col("cumulative_volume").max().alias("cumulative_volume_end"),
            pl.len().alias("tick_count"),
            pl.col("_duplicates_dropped").sum().alias("duplicate_receipts_dropped"),
            pl.col("_out_of_order").sum().alias("out_of_order_tick_count"),
            pl.col("_stale").sum().alias("stale_tick_count"),
            pl.col("_clock_skew").sum().alias("clock_skew_tick_count"),
            pl.col("_received_at").min().alias("first_received_ts"),
            pl.col("_received_at").max().alias("last_received_ts"),
        )
        .sort(["symbol", "_bar_start"])
    )

    output_rows: list[dict[str, Any]] = []
    rejected: list[RejectedBar] = []
    previous_start: dict[str, Any] = {}
    previous_volume: dict[str, float] = {}
    total_missing_minutes = 0

    for row in grouped.to_dicts():
        symbol = str(row["symbol"])
        bar_start = row["_bar_start"]
        missing_before = 0
        if symbol in previous_start:
            missing_before = max(
                0,
                int((bar_start - previous_start[symbol]).total_seconds() // 60) - 1,
            )
        previous_start[symbol] = bar_start
        total_missing_minutes += missing_before

        flags: list[str] = []
        if row["duplicate_receipts_dropped"]:
            flags.append("DUPLICATE_RECEIPT_DROPPED")
        if row["out_of_order_tick_count"]:
            flags.append("OUT_OF_ORDER_TICK")
        if row["stale_tick_count"]:
            flags.append("STALE_TICK")
        if row["clock_skew_tick_count"]:
            flags.append("CLOCK_SKEW")
        if missing_before:
            flags.append("MISSING_PREVIOUS_MINUTE")

        cumulative_end = row["cumulative_volume_end"]
        volume: float | None = None
        if cumulative_end is None:
            flags.append("VOLUME_UNAVAILABLE")
        else:
            cumulative_end = float(cumulative_end)
            prior = previous_volume.get(symbol)
            if prior is None:
                flags.append("VOLUME_BASELINE_MISSING")
            elif cumulative_end < prior:
                flags.append("CUMULATIVE_VOLUME_RESET")
            else:
                volume = cumulative_end - prior
            previous_volume[symbol] = cumulative_end

        core = {
            "provider": "FYERS",
            "symbol": symbol,
            "bar_start": bar_start,
            "bar_end": bar_start + timedelta(minutes=1),
            "first_received_ts": row["first_received_ts"],
            "last_received_ts": row["last_received_ts"],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "bid_close": (
                float(row["bid_close"]) if row["bid_close"] is not None else None
            ),
            "ask_close": (
                float(row["ask_close"]) if row["ask_close"] is not None else None
            ),
            "volume": volume,
            "cumulative_volume_end": cumulative_end,
            "tick_count": int(row["tick_count"]),
            "duplicate_receipts_dropped": int(row["duplicate_receipts_dropped"]),
            "out_of_order_tick_count": int(row["out_of_order_tick_count"]),
            "stale_tick_count": int(row["stale_tick_count"]),
            "clock_skew_tick_count": int(row["clock_skew_tick_count"]),
            "missing_minutes_before": missing_before,
            "quality_flags": sorted(flags),
        }
        hash_payload = json.dumps(
            _json_safe(core),
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            bar = MinuteBar(
                **core,
                bar_hash=hashlib.sha256(hash_payload.encode("utf-8")).hexdigest(),
            )
        except ValidationError as exc:
            rejected.append(
                RejectedBar(
                    symbol=symbol,
                    bar_start=bar_start.isoformat(),
                    reason=str(exc)[:300],
                )
            )
            continue
        output_rows.append(bar.model_dump(mode="json"))

    return BarBuildResult(
        frame=pl.DataFrame(output_rows),
        raw_tick_count=ticks.height,
        deduplicated_tick_count=deduplicated.height,
        duplicate_receipts_dropped=int(
            deduplicated["_duplicates_dropped"].sum() or 0
        ),
        out_of_order_tick_count=int(deduplicated["_out_of_order"].sum() or 0),
        stale_tick_count=int(deduplicated["_stale"].sum() or 0),
        clock_skew_tick_count=int(deduplicated["_clock_skew"].sum() or 0),
        missing_minutes=total_missing_minutes,
        rejected_bars=tuple(rejected),
    )


def write_bar_artifact(
    *,
    project_root: str | Path,
    session_date: str,
    result: BarBuildResult,
) -> BarArtifact:
    if result.frame.is_empty():
        raise ValueError("cannot write an empty bar artifact")
    root = Path(project_root).resolve()
    partition = root / "data" / "derived" / "bars" / f"date={session_date}"
    partition.mkdir(parents=True, exist_ok=True)
    temporary = partition / f".tmp-{uuid.uuid4().hex}.parquet"
    result.frame.write_parquet(temporary, compression="zstd", statistics=True)
    file_hash = _sha256(temporary)
    destination = partition / f"minute-bars-{file_hash[:16]}.parquet"
    if destination.exists():
        temporary.unlink()
    else:
        os.replace(temporary, destination)
    return BarArtifact(
        relative_path=destination.relative_to(root).as_posix(),
        file_sha256=file_hash,
        row_count=result.frame.height,
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
