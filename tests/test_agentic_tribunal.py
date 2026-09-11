import hashlib
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import polars as pl

from stage1.reasoning.agentic_tribunal import (
    decide_candidate,
    run_agentic_tribunal,
    write_tribunal_artifact,
)
from stage1.strategy.candidate_gate import generate_baseline_candidates


def _snapshot(
    *,
    symbol: str = "NSE:RELIANCE-EQ",
    direction: str = "long",
    relative_return_bps: float = 45.0,
    spread: float = 0.10,
    score_bias: float = 0.0,
) -> dict[str, object]:
    observed_at = datetime(2026, 7, 20, 3, 55, tzinfo=timezone.utc)
    long = direction == "long"
    close = 1_500.0 + score_bias
    payload = f"{symbol}|{observed_at.isoformat()}|{direction}|{relative_return_bps}|{spread}"
    return {
        "symbol": symbol,
        "observed_at": observed_at,
        "available_at": observed_at + timedelta(milliseconds=250),
        "close": close,
        "bid": close - spread / 2,
        "ask": close + spread / 2,
        "vwap": close - 2.0 if long else close + 2.0,
        "ema_9": close + 1.0 if long else close - 1.0,
        "ema_21": close - 1.0 if long else close + 1.0,
        "rsi_14": 60.0 if long else 40.0,
        "atr_14": 3.0,
        "volume_z": 2.5,
        "return_5m_bps": relative_return_bps if long else -relative_return_bps,
        "market_return_5m_bps": 0.0,
        "data_quality_flags": [],
        "snapshot_hash": hashlib.sha256(payload.encode()).hexdigest(),
    }


def _candidates(rows: list[dict[str, object]]) -> pl.DataFrame:
    result = generate_baseline_candidates(
        pl.DataFrame(rows),
        trade_symbols=["NSE:RELIANCE-EQ", "NSE:TCS-EQ"],
        candidate_start=time(9, 25),
        entry_cutoff=time(14, 45),
        cadence_minutes=3,
        max_candidates_per_gate=5,
        min_price=100.0,
        max_spread_bps=15.0,
        min_volume_z=0.5,
        cooldown_minutes=15,
    )
    return result.frame


def test_tribunal_allows_strong_long_candidate() -> None:
    frame = run_agentic_tribunal(
        _candidates([_snapshot()]),
        max_spread_bps=15.0,
    )

    assert frame.height == 1
    row = frame.row(0, named=True)
    assert row["action"] == "ALLOW_LONG"
    assert row["exposure"] in {"HALF", "FULL"}
    assert row["confidence_rank"] == row["consensus_score"]
    assert [vote["agent"] for vote in row["votes"]] == ["BULL", "BEAR", "RISK"]


def test_tribunal_holds_when_spread_is_near_limit() -> None:
    frame = _candidates([_snapshot(spread=2.1)])

    decision = decide_candidate(
        frame.row(0, named=True),
        max_spread_bps=15.0,
    )

    assert decision.policy.action == "HOLD"
    assert decision.policy.exposure == "FLAT"
    assert decision.policy.risk_flags == ["SPREAD_NEAR_LIMIT"]


def test_tribunal_decision_id_is_replay_stable() -> None:
    candidate = _candidates([_snapshot(symbol="NSE:TCS-EQ", direction="short")]).row(
        0,
        named=True,
    )

    first = decide_candidate(candidate, max_spread_bps=15.0)
    second = decide_candidate(candidate, max_spread_bps=15.0)

    assert first == second
    assert first.policy.action == "ALLOW_SHORT"


def test_tribunal_artifact_is_content_addressed(tmp_path: Path) -> None:
    decisions = run_agentic_tribunal(
        _candidates([_snapshot()]),
        max_spread_bps=15.0,
    )

    first = write_tribunal_artifact(
        project_root=tmp_path,
        session_date="2026-07-20",
        decisions=decisions,
    )
    second = write_tribunal_artifact(
        project_root=tmp_path,
        session_date="2026-07-20",
        decisions=decisions,
    )

    assert first == second
    assert first.row_count == 1
    artifacts = list((tmp_path / "data" / "derived" / "tribunal").rglob("*.parquet"))
    assert len(artifacts) == 1
