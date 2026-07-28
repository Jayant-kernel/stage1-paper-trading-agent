from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from stage1.schemas import MarketSnapshot


@dataclass(frozen=True)
class FeatureBuildResult:
    frame: pl.DataFrame
    input_bar_count: int
    snapshot_count: int
    warmup_or_incomplete_count: int
    missing_market_context_count: int

    def summary(self) -> dict[str, int]:
        return {
            "input_bar_count": self.input_bar_count,
            "snapshot_count": self.snapshot_count,
            "warmup_or_incomplete_count": self.warmup_or_incomplete_count,
            "missing_market_context_count": self.missing_market_context_count,
        }


@dataclass(frozen=True)
class FeatureArtifact:
    relative_path: str
    file_sha256: str
    row_count: int


def build_market_features(
    bars: pl.DataFrame,
    *,
    market_symbol: str = "NSE:NIFTY50-INDEX",
    warmup_bars: int = 60,
    rsi_period: int = 14,
    atr_period: int = 14,
    volume_window: int = 20,
) -> FeatureBuildResult:
    required = {
        "symbol",
        "bar_start",
        "bar_end",
        "open",
        "high",
        "low",
        "close",
        "bid_close",
        "ask_close",
        "volume",
        "last_received_ts",
        "quality_flags",
        "bar_hash",
    }
    missing = sorted(required - set(bars.columns))
    if missing:
        raise ValueError(f"minute bar frame is missing columns: {missing}")
    if warmup_bars < max(rsi_period + 1, atr_period, volume_window + 1, 6):
        raise ValueError("warmup bars are insufficient for configured features")
    if bars.is_empty():
        return FeatureBuildResult(pl.DataFrame(), 0, 0, 0, 0)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in bars.sort(["symbol", "bar_start"]).to_dicts():
        grouped.setdefault(str(row["symbol"]), []).append(row)
    if market_symbol not in grouped:
        raise ValueError(f"market context symbol is missing: {market_symbol}")

    states_by_symbol = {
        symbol: _feature_states(
            rows,
            warmup_bars=warmup_bars,
            rsi_period=rsi_period,
            atr_period=atr_period,
            volume_window=volume_window,
        )
        for symbol, rows in grouped.items()
    }
    market_returns = {
        state["bar_start"]: state["return_5m_bps"]
        for state in states_by_symbol[market_symbol]
        if state["return_5m_bps"] is not None
    }
    market_availability = {
        state["bar_start"]: state["last_received_ts"]
        for state in states_by_symbol[market_symbol]
    }

    output: list[dict[str, Any]] = []
    incomplete = 0
    missing_market_context = 0
    for symbol in sorted(states_by_symbol):
        for state in states_by_symbol[symbol]:
            required_features = (
                state["vwap"],
                state["ema_9"],
                state["ema_21"],
                state["rsi_14"],
                state["atr_14"],
                state["volume_z"],
                state["return_5m_bps"],
            )
            if not state["warmup_complete"] or any(
                value is None or not math.isfinite(float(value))
                for value in required_features
            ):
                incomplete += 1
                continue
            market_return = market_returns.get(state["bar_start"])
            if market_return is None:
                missing_market_context += 1
                continue
            available_at = max(
                state["bar_end"],
                state["last_received_ts"],
                market_availability[state["bar_start"]],
            )

            flags = sorted(set(state["quality_flags"]))
            snapshot = MarketSnapshot(
                symbol=symbol,
                observed_at=state["bar_end"],
                available_at=available_at,
                close=state["close"],
                bid=state["bid_close"],
                ask=state["ask_close"],
                vwap=state["vwap"],
                ema_9=state["ema_9"],
                ema_21=state["ema_21"],
                rsi_14=state["rsi_14"],
                atr_14=state["atr_14"],
                volume_z=state["volume_z"],
                return_5m_bps=state["return_5m_bps"],
                market_return_5m_bps=float(market_return),
                data_quality_flags=flags,
            )
            snapshot_row = snapshot.model_dump(mode="json")
            snapshot_row["source_bar_hash"] = state["bar_hash"]
            hash_payload = json.dumps(
                snapshot_row,
                sort_keys=True,
                separators=(",", ":"),
            )
            snapshot_row["snapshot_hash"] = hashlib.sha256(
                hash_payload.encode("utf-8")
            ).hexdigest()
            output.append(snapshot_row)

    frame = pl.DataFrame(output)
    if not frame.is_empty():
        frame = frame.sort(["symbol", "observed_at"])
    return FeatureBuildResult(
        frame=frame,
        input_bar_count=bars.height,
        snapshot_count=frame.height,
        warmup_or_incomplete_count=incomplete,
        missing_market_context_count=missing_market_context,
    )


def write_feature_artifact(
    *,
    project_root: str | Path,
    session_date: str,
    result: FeatureBuildResult,
) -> FeatureArtifact:
    if result.frame.is_empty():
        raise ValueError("cannot write an empty feature artifact")
    root = Path(project_root).resolve()
    partition = root / "data" / "derived" / "features" / f"date={session_date}"
    partition.mkdir(parents=True, exist_ok=True)
    temporary = partition / f".tmp-{uuid.uuid4().hex}.parquet"
    result.frame.write_parquet(temporary, compression="zstd", statistics=True)
    file_hash = _sha256(temporary)
    destination = partition / f"market-features-{file_hash[:16]}.parquet"
    if destination.exists():
        temporary.unlink()
    else:
        os.replace(temporary, destination)
    return FeatureArtifact(
        relative_path=destination.relative_to(root).as_posix(),
        file_sha256=file_hash,
        row_count=result.frame.height,
    )


def _feature_states(
    rows: list[dict[str, Any]],
    *,
    warmup_bars: int,
    rsi_period: int,
    atr_period: int,
    volume_window: int,
) -> list[dict[str, Any]]:
    ema_9: float | None = None
    ema_21: float | None = None
    average_gain: float | None = None
    average_loss: float | None = None
    atr: float | None = None
    gains: list[float] = []
    losses: list[float] = []
    true_ranges: list[float] = []
    volume_history: list[float] = []
    vwap_numerator = 0.0
    vwap_denominator = 0.0
    states: list[dict[str, Any]] = []

    for index, row in enumerate(rows):
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        bar_start = _timestamp(row["bar_start"])
        bar_end = _timestamp(row["bar_end"])
        previous_close = float(rows[index - 1]["close"]) if index else None

        ema_9 = _ema(close, ema_9, span=9)
        ema_21 = _ema(close, ema_21, span=21)

        if previous_close is None:
            true_range = high - low
        else:
            true_range = max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )
        true_ranges.append(true_range)
        if len(true_ranges) == atr_period:
            atr = statistics.fmean(true_ranges)
        elif len(true_ranges) > atr_period and atr is not None:
            atr = ((atr * (atr_period - 1)) + true_range) / atr_period

        rsi: float | None = None
        if previous_close is not None:
            change = close - previous_close
            gain = max(change, 0.0)
            loss = max(-change, 0.0)
            gains.append(gain)
            losses.append(loss)
            if len(gains) == rsi_period:
                average_gain = statistics.fmean(gains)
                average_loss = statistics.fmean(losses)
            elif len(gains) > rsi_period and average_gain is not None and average_loss is not None:
                average_gain = ((average_gain * (rsi_period - 1)) + gain) / rsi_period
                average_loss = ((average_loss * (rsi_period - 1)) + loss) / rsi_period
            if average_gain is not None and average_loss is not None:
                if average_loss == 0:
                    rsi = 100.0 if average_gain > 0 else 50.0
                else:
                    relative_strength = average_gain / average_loss
                    rsi = 100.0 - (100.0 / (1.0 + relative_strength))

        flags = list(row.get("quality_flags") or [])
        volume = row.get("volume")
        if volume is None:
            current_volume = None
            volume_z = 0.0
            flags.append("VOLUME_FEATURE_UNAVAILABLE")
        else:
            current_volume = float(volume)
            history = volume_history[-volume_window:]
            if len(history) < 5:
                volume_z = None
            else:
                mean = statistics.fmean(history)
                deviation = statistics.pstdev(history)
                volume_z = 0.0 if deviation == 0 else (current_volume - mean) / deviation
            volume_history.append(current_volume)
            vwap_numerator += close * current_volume
            vwap_denominator += current_volume
        vwap = vwap_numerator / vwap_denominator if vwap_denominator > 0 else close

        return_5m: float | None = None
        if index >= 5:
            comparison = rows[index - 5]
            comparison_start = _timestamp(comparison["bar_start"])
            if bar_start - comparison_start == timedelta(minutes=5):
                return_5m = (close / float(comparison["close"]) - 1.0) * 10_000
            else:
                flags.append("RETURN_WINDOW_GAPPED")

        states.append(
            {
                "symbol": str(row["symbol"]),
                "bar_start": bar_start,
                "bar_end": bar_end,
                "last_received_ts": _timestamp(row["last_received_ts"]),
                "close": close,
                "bid_close": row.get("bid_close"),
                "ask_close": row.get("ask_close"),
                "vwap": vwap,
                "ema_9": ema_9,
                "ema_21": ema_21,
                "rsi_14": rsi,
                "atr_14": atr,
                "volume_z": volume_z,
                "return_5m_bps": return_5m,
                "quality_flags": flags,
                "bar_hash": str(row["bar_hash"]),
                "warmup_complete": index + 1 >= warmup_bars,
            }
        )
    return states


def _ema(value: float, previous: float | None, *, span: int) -> float:
    if previous is None:
        return value
    alpha = 2.0 / (span + 1.0)
    return (alpha * value) + ((1.0 - alpha) * previous)


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
