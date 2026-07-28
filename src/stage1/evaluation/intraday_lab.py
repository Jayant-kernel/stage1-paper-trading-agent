from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

import polars as pl
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stage1.config import Stage1Config
from stage1.paper.costs import CostTable
from stage1.paper.replay import prepare_replay_data, run_paper_replay
from stage1.strategy.candidate_gate import CandidateRule, generate_rule_candidates


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IntradaySession(_FrozenModel):
    session_date: date
    role: Literal["DEVELOPMENT", "VALIDATION"]
    bar_artifact: str
    feature_artifact: str


class IntradayVariant(_FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{2,63}$")
    description: str = Field(min_length=1)
    entry_cutoff: time
    max_spread_bps: float = Field(gt=0)
    min_volume_z: float
    relative_momentum_bps: float = Field(ge=0)
    long_rsi: tuple[float, float] | None
    short_rsi: tuple[float, float] | None

    @field_validator("long_rsi", "short_rsi")
    @classmethod
    def rsi_bounds_are_ordered(
        cls, value: tuple[float, float] | None
    ) -> tuple[float, float] | None:
        if value is not None and not 0 <= value[0] <= value[1] <= 100:
            raise ValueError("RSI bounds must be ordered within 0-100")
        return value

    def candidate_rule(self) -> CandidateRule:
        return CandidateRule(
            version=self.id,
            relative_momentum_bps=self.relative_momentum_bps,
            long_rsi_min=None if self.long_rsi is None else self.long_rsi[0],
            long_rsi_max=None if self.long_rsi is None else self.long_rsi[1],
            short_rsi_min=None if self.short_rsi is None else self.short_rsi[0],
            short_rsi_max=None if self.short_rsi is None else self.short_rsi[1],
        )


class IntradayProtocol(_FrozenModel):
    protocol_version: Literal["intraday_variant_lab_v1"]
    test_window: Literal["SEALED"]
    sessions: tuple[IntradaySession, ...] = Field(min_length=2)
    artificial_delay_seconds: tuple[float, ...] = Field(min_length=1)
    variants: tuple[IntradayVariant, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def protocol_is_complete(self) -> "IntradayProtocol":
        if len({item.id for item in self.variants}) != len(self.variants):
            raise ValueError("variant IDs must be unique")
        if len({item.session_date for item in self.sessions}) != len(self.sessions):
            raise ValueError("session dates must be unique")
        if {item.role for item in self.sessions} != {"DEVELOPMENT", "VALIDATION"}:
            raise ValueError("protocol needs development and validation sessions")
        if any(value < 0 for value in self.artificial_delay_seconds):
            raise ValueError("artificial delays cannot be negative")
        if sorted(set(self.artificial_delay_seconds)) != list(
            self.artificial_delay_seconds
        ):
            raise ValueError("artificial delays must be unique and increasing")
        return self


@dataclass(frozen=True)
class IntradayLabArtifact:
    relative_path: str
    sha256: str


def load_intraday_protocol(
    path: str | Path, *, stage1: Stage1Config
) -> IntradayProtocol:
    protocol_path = Path(path)
    raw = yaml.safe_load(protocol_path.read_text(encoding="utf-8"))
    protocol = IntradayProtocol.model_validate(raw)
    if len(protocol.variants) > stage1.evaluation.max_declared_variants:
        raise ValueError("declared variants exceed the Stage 1 maximum")
    if protocol.artificial_delay_seconds != tuple(
        stage1.evaluation.artificial_delay_seconds
    ):
        raise ValueError("protocol must use the frozen Stage 1 artificial delays")
    return protocol


def apply_artificial_delay(candidates: pl.DataFrame, delay_seconds: float) -> pl.DataFrame:
    if delay_seconds == 0 or candidates.is_empty():
        return candidates.clone()
    output: list[dict[str, Any]] = []
    for row in candidates.to_dicts():
        original_created_at = row["created_at"]
        created_at = _timestamp(original_created_at) + timedelta(seconds=delay_seconds)
        valid_until = _timestamp(row["valid_until"])
        if created_at >= valid_until:
            continue
        original_id = str(row["candidate_id"])
        row["created_at"] = (
            created_at.isoformat()
            if isinstance(original_created_at, str)
            else created_at
        )
        row["candidate_id"] = _json_hash(
            {
                "original_candidate_id": original_id,
                "artificial_delay_seconds": delay_seconds,
            }
        )
        row["deterministic_reasons"] = list(row["deterministic_reasons"]) + [
            f"artificial_delay_seconds:{delay_seconds:g}"
        ]
        output.append(row)
    return pl.DataFrame(output, schema=candidates.schema) if output else candidates.head(0)


def run_intraday_lab(
    *,
    project_root: str | Path,
    stage1: Stage1Config,
    protocol: IntradayProtocol,
    cost_table: CostTable,
    protocol_sha256: str,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    sectors = _load_sectors(root / "config" / "universe.csv")
    results: list[dict[str, Any]] = []
    session_evidence: list[dict[str, Any]] = []

    for session in protocol.sessions:
        bar_path = _safe_artifact(root, session.bar_artifact)
        feature_path = _safe_artifact(root, session.feature_artifact)
        tick_files = sorted(
            (root / "data" / "raw" / "ticks" / f"date={session.session_date}").rglob(
                "*.parquet"
            )
        )
        if not tick_files:
            raise ValueError(f"no raw ticks for {session.session_date}")
        bars = pl.read_parquet(bar_path)
        features = pl.read_parquet(feature_path)
        ticks = pl.read_parquet(tick_files)
        prepared = prepare_replay_data(features=features, ticks=ticks, bars=bars)
        coverage = ticks.select(
            pl.col("received_ts").min().alias("first_received_at"),
            pl.col("received_ts").max().alias("last_received_at"),
            pl.col("symbol").n_unique().alias("symbol_count"),
            pl.len().alias("raw_rows"),
        ).to_dicts()[0]
        session_evidence.append(
            {
                "session_date": str(session.session_date),
                "role": session.role,
                "bar_artifact": session.bar_artifact,
                "bar_sha256": _file_sha256(bar_path),
                "feature_artifact": session.feature_artifact,
                "feature_sha256": _file_sha256(feature_path),
                **{key: _json_value(value) for key, value in coverage.items()},
            }
        )

        for variant in protocol.variants:
            generated = generate_rule_candidates(
                features,
                trade_symbols=stage1.universe.symbols,
                candidate_start=stage1.market.candidate_start,
                entry_cutoff=variant.entry_cutoff,
                cadence_minutes=stage1.candidate_gate.cadence_minutes,
                max_candidates_per_gate=stage1.candidate_gate.max_candidates,
                min_price=stage1.candidate_gate.min_price,
                max_spread_bps=variant.max_spread_bps,
                min_volume_z=variant.min_volume_z,
                cooldown_minutes=stage1.candidate_gate.cooldown_minutes_after_decision,
                rule=variant.candidate_rule(),
            )
            for delay in protocol.artificial_delay_seconds:
                candidates = apply_artificial_delay(generated.frame, delay)
                if candidates.is_empty():
                    for profile in stage1.paper.cost_profiles:
                        results.append(
                            _empty_result(
                                session=session,
                                variant=variant,
                                delay=delay,
                                profile=profile,
                                candidate_count=0,
                            )
                        )
                    continue
                replay = run_paper_replay(
                    candidates=candidates,
                    features=features,
                    ticks=ticks,
                    bars=bars,
                    sectors=sectors,
                    paper=stage1.paper,
                    cost_table=cost_table,
                    flatten_at=stage1.market.flatten,
                    stale_tick_seconds=stage1.data.stale_tick_seconds,
                    prepared_data=prepared,
                )
                for profile in stage1.paper.cost_profiles:
                    results.append(
                        _summarize_result(
                            session=session,
                            variant=variant,
                            delay=delay,
                            profile=profile,
                            replay=replay,
                        )
                    )

    development_ranking = _development_ranking(results)
    zero_delay = [row for row in results if row["artificial_delay_seconds"] == 0]
    positive_everywhere = [
        variant.id
        for variant in protocol.variants
        if all(
            row["net_pnl"] > 0 and row["completed_trades"] > 0
            for row in zero_delay
            if row["variant_id"] == variant.id
        )
    ]
    return {
        "protocol_version": protocol.protocol_version,
        "protocol_sha256": protocol_sha256,
        "test_window": protocol.test_window,
        "test_window_opened": False,
        "sessions": session_evidence,
        "variants": [item.model_dump(mode="json") for item in protocol.variants],
        "results": results,
        "development_ranking_zero_delay": development_ranking,
        "diagnostics": {
            "positive_on_both_sessions_and_profiles_zero_delay": positive_everywhere,
            "result_rows": len(results),
        },
        "gate": {
            "status": "INSUFFICIENT_SESSIONS",
            "promotion_allowed": False,
            "live_config_changed": False,
            "reason": (
                "Only one development and one validation session exist; results are "
                "descriptive and cannot establish generalization."
            ),
        },
    }


def write_intraday_lab_artifact(
    *, project_root: str | Path, report: dict[str, Any]
) -> IntradayLabArtifact:
    root = Path(project_root).resolve()
    payload = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
    digest = hashlib.sha256(payload).hexdigest()
    directory = root / "data" / "evaluation"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"intraday-variant-lab-{digest[:16]}.json"
    if not destination.exists():
        temporary = directory / f".tmp-{uuid.uuid4().hex}.json"
        temporary.write_bytes(payload)
        os.replace(temporary, destination)
    return IntradayLabArtifact(
        relative_path=destination.relative_to(root).as_posix(), sha256=digest
    )


def _summarize_result(
    *, session: IntradaySession, variant: IntradayVariant, delay: float, profile: str, replay: Any
) -> dict[str, Any]:
    if replay.outcomes.is_empty():
        return _empty_result(
            session=session,
            variant=variant,
            delay=delay,
            profile=profile,
            candidate_count=replay.candidate_count,
            approved_count=replay.approved_count,
            entry_fill_count=replay.entry_fill_count,
        )
    outcomes = replay.outcomes.filter(pl.col("cost_profile") == profile)
    net = outcomes["net_pnl"].to_list()
    return {
        "session_date": str(session.session_date),
        "role": session.role,
        "variant_id": variant.id,
        "artificial_delay_seconds": delay,
        "cost_profile": profile,
        "candidate_count": replay.candidate_count,
        "approved_count": replay.approved_count,
        "entry_fill_count": replay.entry_fill_count,
        "completed_trades": outcomes.height,
        "wins": sum(value > 0 for value in net),
        "win_rate": round(sum(value > 0 for value in net) / outcomes.height, 6),
        "gross_pnl": round(float(outcomes["gross_pnl"].sum()), 4),
        "explicit_costs": round(float(outcomes["explicit_costs"].sum()), 4),
        "net_pnl": round(float(sum(net)), 4),
        "worst_trade_net_pnl": round(float(min(net)), 4),
        "ending_equity": replay.ending_equity_by_profile[profile],
        "unresolved_positions": replay.unresolved_position_count,
    }


def _empty_result(
    *,
    session: IntradaySession,
    variant: IntradayVariant,
    delay: float,
    profile: str,
    candidate_count: int,
    approved_count: int = 0,
    entry_fill_count: int = 0,
) -> dict[str, Any]:
    return {
        "session_date": str(session.session_date),
        "role": session.role,
        "variant_id": variant.id,
        "artificial_delay_seconds": delay,
        "cost_profile": profile,
        "candidate_count": candidate_count,
        "approved_count": approved_count,
        "entry_fill_count": entry_fill_count,
        "completed_trades": 0,
        "wins": 0,
        "win_rate": 0.0,
        "gross_pnl": 0.0,
        "explicit_costs": 0.0,
        "net_pnl": 0.0,
        "worst_trade_net_pnl": 0.0,
        "ending_equity": 100000.0,
        "unresolved_positions": 0,
    }


def _development_ranking(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        row
        for row in results
        if row["role"] == "DEVELOPMENT" and row["artificial_delay_seconds"] == 0
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["variant_id"], []).append(row)
    ranking = [
        {
            "variant_id": variant_id,
            "worst_profile_net_pnl": min(row["net_pnl"] for row in values),
            "completed_trades": min(row["completed_trades"] for row in values),
        }
        for variant_id, values in grouped.items()
    ]
    ranking.sort(
        key=lambda row: (
            -row["worst_profile_net_pnl"],
            -row["completed_trades"],
            row["variant_id"],
        )
    )
    for index, row in enumerate(ranking, start=1):
        row["descriptive_rank"] = index
    return ranking


def _load_sectors(path: Path) -> dict[str, str]:
    import csv

    with path.open(newline="", encoding="utf-8") as handle:
        return {row["symbol"]: row["sector"] for row in csv.DictReader(handle)}


def _safe_artifact(root: Path, relative_path: str) -> Path:
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"invalid session artifact: {relative_path}")
    return path


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_value(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (datetime, date)) else value
