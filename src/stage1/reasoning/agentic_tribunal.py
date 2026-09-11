from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import polars as pl

from stage1.schemas import ModelPolicy


TribunalAction = Literal["HOLD", "ALLOW_LONG", "ALLOW_SHORT"]


@dataclass(frozen=True)
class AgentVote:
    agent: Literal["BULL", "BEAR", "RISK"]
    stance: Literal["SUPPORT", "OPPOSE", "RISK_HOLD"]
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class TribunalDecision:
    policy: ModelPolicy
    decision_id: str
    candidate_id: str
    votes: tuple[AgentVote, ...]
    consensus_score: int


@dataclass(frozen=True)
class TribunalArtifact:
    relative_path: str
    file_sha256: str
    row_count: int


def run_agentic_tribunal(
    candidates: pl.DataFrame,
    *,
    max_spread_bps: float,
    min_consensus_score: int = 55,
    full_exposure_score: int = 80,
) -> pl.DataFrame:
    """Evaluate deterministic candidates with a replayable bull/bear/risk debate."""

    if min_consensus_score < 0 or full_exposure_score < min_consensus_score:
        raise ValueError("consensus thresholds must be ordered and non-negative")
    required = {
        "candidate_id",
        "symbol",
        "side",
        "created_at",
        "valid_until",
        "score",
        "spread_bps",
        "relative_return_5m_bps",
        "snapshot_hash",
        "deterministic_reasons",
    }
    missing = sorted(required - set(candidates.columns))
    if missing:
        raise ValueError(f"candidate frame is missing columns: {missing}")
    if candidates.is_empty():
        return pl.DataFrame()

    rows = []
    for row in candidates.sort(["created_at", "symbol", "candidate_id"]).to_dicts():
        decision = decide_candidate(
            row,
            max_spread_bps=max_spread_bps,
            min_consensus_score=min_consensus_score,
            full_exposure_score=full_exposure_score,
        )
        rows.append(
            {
                "decision_id": decision.decision_id,
                "candidate_id": decision.candidate_id,
                "symbol": decision.policy.symbol,
                "action": decision.policy.action,
                "exposure": decision.policy.exposure,
                "confidence_rank": decision.policy.confidence_rank,
                "consensus_score": decision.consensus_score,
                "risk_flags": decision.policy.risk_flags,
                "invalidation_conditions": decision.policy.invalidation_conditions,
                "valid_until": decision.policy.valid_until,
                "votes": [
                    {
                        "agent": vote.agent,
                        "stance": vote.stance,
                        "score": vote.score,
                        "reasons": list(vote.reasons),
                    }
                    for vote in decision.votes
                ],
            }
        )
    return pl.DataFrame(rows)


def write_tribunal_artifact(
    *,
    project_root: str | Path,
    session_date: str,
    decisions: pl.DataFrame,
) -> TribunalArtifact:
    if decisions.is_empty():
        raise ValueError("cannot write an empty tribunal artifact")
    root = Path(project_root).resolve()
    partition = root / "data" / "derived" / "tribunal" / f"date={session_date}"
    partition.mkdir(parents=True, exist_ok=True)
    temporary = partition / f".tmp-{uuid.uuid4().hex}.parquet"
    decisions.write_parquet(temporary, compression="zstd", statistics=True)
    file_hash = _sha256(temporary)
    destination = partition / f"tribunal-decisions-{file_hash[:16]}.parquet"
    if destination.exists():
        temporary.unlink()
    else:
        os.replace(temporary, destination)
    return TribunalArtifact(
        relative_path=destination.relative_to(root).as_posix(),
        file_sha256=file_hash,
        row_count=decisions.height,
    )


def decide_candidate(
    candidate: dict[str, Any],
    *,
    max_spread_bps: float,
    min_consensus_score: int = 55,
    full_exposure_score: int = 80,
) -> TribunalDecision:
    if max_spread_bps <= 0:
        raise ValueError("max spread must be positive")
    candidate_id = str(candidate["candidate_id"])
    side = str(candidate["side"])
    if side not in {"LONG_CANDIDATE", "SHORT_CANDIDATE"}:
        raise ValueError(f"unsupported candidate side: {side}")

    votes = (
        _bull_vote(candidate),
        _bear_vote(candidate),
        _risk_vote(candidate, max_spread_bps=max_spread_bps),
    )
    consensus_score = max(
        0,
        min(
            100,
            round(
                (votes[0].score * 0.45)
                + (votes[1].score * 0.25)
                + (votes[2].score * 0.30)
            ),
        ),
    )
    risk_flags = _risk_flags(candidate, max_spread_bps=max_spread_bps)
    action: TribunalAction
    exposure: Literal["FLAT", "HALF", "FULL"]
    if risk_flags or consensus_score < min_consensus_score:
        action = "HOLD"
        exposure = "FLAT"
    else:
        action = "ALLOW_LONG" if side == "LONG_CANDIDATE" else "ALLOW_SHORT"
        exposure = "FULL" if consensus_score >= full_exposure_score else "HALF"

    policy = ModelPolicy(
        symbol=str(candidate["symbol"]),
        action=action,
        exposure=exposure,
        evidence_ids=[str(candidate["snapshot_hash"])],
        risk_flags=risk_flags,
        invalidation_conditions=_invalidation_conditions(side),
        confidence_rank=consensus_score,
        valid_until=_timestamp(candidate["valid_until"]),
    )
    decision_id = _decision_id(candidate_id, policy, votes, consensus_score)
    return TribunalDecision(
        policy=policy,
        decision_id=decision_id,
        candidate_id=candidate_id,
        votes=votes,
        consensus_score=consensus_score,
    )


def _bull_vote(candidate: dict[str, Any]) -> AgentVote:
    score = int(round(float(candidate["score"]) * 100))
    relative = float(candidate["relative_return_5m_bps"])
    reasons = ["candidate_gate_passed"]
    if abs(relative) >= 25:
        score += 8
        reasons.append("strong_relative_momentum")
    if "volume_z_threshold" in set(candidate.get("deterministic_reasons") or []):
        score += 5
        reasons.append("volume_confirmation")
    return AgentVote(
        agent="BULL",
        stance="SUPPORT",
        score=min(score, 100),
        reasons=tuple(reasons),
    )


def _bear_vote(candidate: dict[str, Any]) -> AgentVote:
    score = 100 - int(round(float(candidate["score"]) * 100))
    relative = abs(float(candidate["relative_return_5m_bps"]))
    spread = float(candidate["spread_bps"])
    reasons = []
    if relative < 20:
        score += 18
        reasons.append("thin_relative_edge")
    if spread > 10:
        score += 12
        reasons.append("spread_drag")
    if not reasons:
        reasons.append("no_major_counter_signal")
    return AgentVote(
        agent="BEAR",
        stance="OPPOSE",
        score=max(0, min(100, 100 - score)),
        reasons=tuple(reasons),
    )


def _risk_vote(candidate: dict[str, Any], *, max_spread_bps: float) -> AgentVote:
    score = int(round(float(candidate["score"]) * 100))
    spread_ratio = float(candidate["spread_bps"]) / max_spread_bps
    reasons = ["paper_only_candidate"]
    if spread_ratio > 0.80:
        score -= 40
        reasons.append("spread_near_limit")
    if abs(float(candidate["relative_return_5m_bps"])) > 80:
        score -= 20
        reasons.append("extended_move_chase_risk")
    if score >= 55:
        stance: Literal["SUPPORT", "OPPOSE", "RISK_HOLD"] = "SUPPORT"
    else:
        stance = "RISK_HOLD"
    return AgentVote(
        agent="RISK",
        stance=stance,
        score=max(0, min(100, score)),
        reasons=tuple(reasons),
    )


def _risk_flags(candidate: dict[str, Any], *, max_spread_bps: float) -> list[str]:
    flags = []
    spread_bps = float(candidate["spread_bps"])
    relative_bps = abs(float(candidate["relative_return_5m_bps"]))
    if spread_bps > max_spread_bps:
        flags.append("SPREAD_OVER_LIMIT")
    elif spread_bps / max_spread_bps > 0.90:
        flags.append("SPREAD_NEAR_LIMIT")
    if relative_bps > 100:
        flags.append("EXTENDED_MOVE_CHASE_RISK")
    return flags


def _invalidation_conditions(side: str) -> list[str]:
    direction = "falls below" if side == "LONG_CANDIDATE" else "rises above"
    return [
        f"relative momentum {direction} market",
        "spread breaches configured limit",
        "snapshot expires before next observable fill",
    ]


def _decision_id(
    candidate_id: str,
    policy: ModelPolicy,
    votes: tuple[AgentVote, ...],
    consensus_score: int,
) -> str:
    payload = {
        "candidate_id": candidate_id,
        "policy": policy.model_dump(mode="json"),
        "votes": [
            {
                "agent": vote.agent,
                "stance": vote.stance,
                "score": vote.score,
                "reasons": vote.reasons,
            }
            for vote in votes
        ],
        "consensus_score": consensus_score,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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
