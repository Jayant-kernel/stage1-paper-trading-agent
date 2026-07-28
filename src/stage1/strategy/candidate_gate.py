from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import polars as pl

from stage1.schemas import DeterministicCandidate

_IST = ZoneInfo("Asia/Kolkata")
_CRITICAL_FLAGS = frozenset(
    {
        "OUT_OF_ORDER_TICK",
        "STALE_TICK",
        "CLOCK_SKEW",
        "MISSING_PREVIOUS_MINUTE",
        "CUMULATIVE_VOLUME_RESET",
        "RETURN_WINDOW_GAPPED",
    }
)


@dataclass(frozen=True)
class CandidateGateResult:
    frame: pl.DataFrame
    evaluated_snapshots: int
    qualified_before_ranking: int
    blocked_by_quality: int
    blocked_by_cooldown: int

    def summary(self) -> dict[str, int]:
        return {
            "evaluated_snapshots": self.evaluated_snapshots,
            "qualified_before_ranking": self.qualified_before_ranking,
            "candidate_count": self.frame.height,
            "blocked_by_quality": self.blocked_by_quality,
            "blocked_by_cooldown": self.blocked_by_cooldown,
        }


@dataclass(frozen=True)
class CandidateArtifact:
    relative_path: str
    file_sha256: str
    row_count: int


@dataclass(frozen=True)
class CandidateRule:
    """Frozen, explainable signal thresholds used by an offline replay variant."""

    version: str = "baseline_v1"
    relative_momentum_bps: float = 10.0
    long_rsi_min: float | None = 52.0
    long_rsi_max: float | None = 72.0
    short_rsi_min: float | None = 28.0
    short_rsi_max: float | None = 48.0

    def __post_init__(self) -> None:
        if not self.version or len(self.version) > 64:
            raise ValueError("candidate rule version must contain 1-64 characters")
        if self.relative_momentum_bps < 0:
            raise ValueError("relative momentum threshold cannot be negative")
        for lower, upper, label in (
            (self.long_rsi_min, self.long_rsi_max, "long"),
            (self.short_rsi_min, self.short_rsi_max, "short"),
        ):
            if (lower is None) != (upper is None):
                raise ValueError(f"{label} RSI bounds must both be set or both disabled")
            if lower is not None and not 0 <= lower <= upper <= 100:
                raise ValueError(f"{label} RSI bounds must be ordered within 0-100")


def generate_baseline_candidates(
    snapshots: pl.DataFrame,
    *,
    trade_symbols: Iterable[str],
    candidate_start: time,
    entry_cutoff: time,
    cadence_minutes: int,
    max_candidates_per_gate: int,
    min_price: float,
    max_spread_bps: float,
    min_volume_z: float,
    cooldown_minutes: int,
) -> CandidateGateResult:
    return generate_rule_candidates(
        snapshots,
        trade_symbols=trade_symbols,
        candidate_start=candidate_start,
        entry_cutoff=entry_cutoff,
        cadence_minutes=cadence_minutes,
        max_candidates_per_gate=max_candidates_per_gate,
        min_price=min_price,
        max_spread_bps=max_spread_bps,
        min_volume_z=min_volume_z,
        cooldown_minutes=cooldown_minutes,
        rule=CandidateRule(),
    )


def generate_rule_candidates(
    snapshots: pl.DataFrame,
    *,
    trade_symbols: Iterable[str],
    candidate_start: time,
    entry_cutoff: time,
    cadence_minutes: int,
    max_candidates_per_gate: int,
    min_price: float,
    max_spread_bps: float,
    min_volume_z: float,
    cooldown_minutes: int,
    rule: CandidateRule,
) -> CandidateGateResult:
    if cadence_minutes <= 0 or max_candidates_per_gate < 0 or cooldown_minutes < 0:
        raise ValueError("candidate timing and limits must be non-negative")
    required = {
        "symbol",
        "observed_at",
        "available_at",
        "close",
        "bid",
        "ask",
        "vwap",
        "ema_9",
        "ema_21",
        "rsi_14",
        "atr_14",
        "volume_z",
        "return_5m_bps",
        "market_return_5m_bps",
        "data_quality_flags",
        "snapshot_hash",
    }
    missing = sorted(required - set(snapshots.columns))
    if missing:
        raise ValueError(f"feature frame is missing columns: {missing}")

    allowed_symbols = frozenset(trade_symbols)
    gates: dict[datetime, list[dict[str, Any]]] = {}
    evaluated = 0
    qualified = 0
    blocked_quality = 0
    for row in snapshots.sort(["observed_at", "symbol"]).to_dicts():
        symbol = str(row["symbol"])
        if symbol not in allowed_symbols:
            continue
        observed_at = _timestamp(row["observed_at"])
        available_at = _timestamp(row["available_at"])
        observed_ist = observed_at.astimezone(_IST)
        if not candidate_start <= observed_ist.time() <= entry_cutoff:
            continue
        minutes_from_start = int(
            (
                datetime.combine(observed_ist.date(), observed_ist.time(), tzinfo=_IST)
                - datetime.combine(observed_ist.date(), candidate_start, tzinfo=_IST)
            ).total_seconds()
            // 60
        )
        if minutes_from_start % cadence_minutes:
            continue
        evaluated += 1

        flags = set(row.get("data_quality_flags") or [])
        if flags & _CRITICAL_FLAGS:
            blocked_quality += 1
            continue
        close = float(row["close"])
        bid = row.get("bid")
        ask = row.get("ask")
        if close < min_price or bid is None or ask is None:
            continue
        bid = float(bid)
        ask = float(ask)
        midpoint = (bid + ask) / 2.0
        if midpoint <= 0 or ask < bid:
            blocked_quality += 1
            continue
        spread_bps = ((ask - bid) / midpoint) * 10_000
        if spread_bps > max_spread_bps or float(row["volume_z"]) < min_volume_z:
            continue

        relative_return = float(row["return_5m_bps"]) - float(
            row["market_return_5m_bps"]
        )
        rsi = float(row["rsi_14"])
        long_rsi_ok = (
            True
            if rule.long_rsi_min is None
            else rule.long_rsi_min <= rsi <= rule.long_rsi_max
        )
        short_rsi_ok = (
            True
            if rule.short_rsi_min is None
            else rule.short_rsi_min <= rsi <= rule.short_rsi_max
        )
        long_signal = (
            close > float(row["vwap"])
            and float(row["ema_9"]) > float(row["ema_21"])
            and relative_return > rule.relative_momentum_bps
            and long_rsi_ok
        )
        short_signal = (
            close < float(row["vwap"])
            and float(row["ema_9"]) < float(row["ema_21"])
            and relative_return < -rule.relative_momentum_bps
            and short_rsi_ok
        )
        if not long_signal and not short_signal:
            continue
        qualified += 1
        side = "LONG_CANDIDATE" if long_signal else "SHORT_CANDIDATE"
        score = _score(
            relative_return=relative_return,
            volume_z=float(row["volume_z"]),
            ema_9=float(row["ema_9"]),
            ema_21=float(row["ema_21"]),
            atr=float(row["atr_14"]),
            spread_bps=spread_bps,
            max_spread_bps=max_spread_bps,
        )
        gates.setdefault(observed_at, []).append(
            {
                "symbol": symbol,
                "side": side,
                "created_at": available_at,
                "score": score,
                "spread_bps": spread_bps,
                "relative_return_5m_bps": relative_return,
                "snapshot_hash": str(row["snapshot_hash"]),
                "deterministic_reasons": [
                    f"variant:{rule.version}",
                    "price_vs_vwap_aligned",
                    "ema_9_21_aligned",
                    "relative_5m_momentum_threshold",
                    "rsi_filter_disabled"
                    if rule.long_rsi_min is None
                    else "rsi_in_declared_band",
                    "volume_z_threshold",
                    "spread_within_limit",
                ],
            }
        )

    output: list[dict[str, Any]] = []
    last_decision: dict[str, datetime] = {}
    blocked_cooldown = 0
    for gate_time in sorted(gates):
        eligible: list[dict[str, Any]] = []
        for item in gates[gate_time]:
            prior = last_decision.get(item["symbol"])
            if prior is not None and gate_time - prior < timedelta(minutes=cooldown_minutes):
                blocked_cooldown += 1
                continue
            eligible.append(item)
        selected = sorted(
            eligible,
            key=lambda item: (-item["score"], item["symbol"]),
        )[:max_candidates_per_gate]
        for item in selected:
            valid_until = item["created_at"] + timedelta(minutes=cadence_minutes)
            identity = {
                "candidate_version": rule.version,
                "snapshot_hash": item["snapshot_hash"],
                "side": item["side"],
                "valid_until": valid_until.isoformat(),
            }
            candidate_id = hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            candidate = DeterministicCandidate(
                candidate_version=rule.version,
                candidate_id=candidate_id,
                valid_until=valid_until,
                **item,
            )
            output.append(candidate.model_dump(mode="json"))
            last_decision[item["symbol"]] = gate_time

    frame = pl.DataFrame(output)
    if not frame.is_empty():
        frame = frame.sort(["created_at", "score", "symbol"], descending=[False, True, False])
    return CandidateGateResult(
        frame=frame,
        evaluated_snapshots=evaluated,
        qualified_before_ranking=qualified,
        blocked_by_quality=blocked_quality,
        blocked_by_cooldown=blocked_cooldown,
    )


def write_candidate_artifact(
    *,
    project_root: str | Path,
    session_date: str,
    result: CandidateGateResult,
) -> CandidateArtifact:
    if result.frame.is_empty():
        raise ValueError("cannot write an empty candidate artifact")
    root = Path(project_root).resolve()
    partition = root / "data" / "derived" / "candidates" / f"date={session_date}"
    partition.mkdir(parents=True, exist_ok=True)
    temporary = partition / f".tmp-{uuid.uuid4().hex}.parquet"
    result.frame.write_parquet(temporary, compression="zstd", statistics=True)
    file_hash = _sha256(temporary)
    destination = partition / f"baseline-candidates-{file_hash[:16]}.parquet"
    if destination.exists():
        temporary.unlink()
    else:
        os.replace(temporary, destination)
    return CandidateArtifact(
        relative_path=destination.relative_to(root).as_posix(),
        file_sha256=file_hash,
        row_count=result.frame.height,
    )


def _score(
    *,
    relative_return: float,
    volume_z: float,
    ema_9: float,
    ema_21: float,
    atr: float,
    spread_bps: float,
    max_spread_bps: float,
) -> float:
    relative_momentum = min(abs(relative_return) / 50.0, 1.0)
    clipped_volume = min(max(volume_z, 0.0) / 3.0, 1.0)
    trend_alignment = min(abs(ema_9 - ema_21) / max(atr, 1e-9), 1.0)
    spread_quality = max(0.0, 1.0 - (spread_bps / max_spread_bps))
    return round(
        (0.30 * relative_momentum)
        + (0.25 * clipped_volume)
        + (0.20 * trend_alignment)
        + (0.15 * spread_quality),
        10,
    )


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
