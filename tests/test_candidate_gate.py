import hashlib
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import polars as pl

from stage1.strategy.candidate_gate import (
    CandidateRule,
    generate_baseline_candidates,
    generate_rule_candidates,
    write_candidate_artifact,
)


def _snapshot(
    *,
    symbol: str = "NSE:RELIANCE-EQ",
    minute: int = 0,
    direction: str = "long",
    flags: list[str] | None = None,
    score_bias: float = 0.0,
) -> dict[str, object]:
    # 03:55 UTC is 09:25 IST, the first frozen candidate gate.
    observed_at = datetime(2026, 7, 20, 3, 55, tzinfo=timezone.utc) + timedelta(
        minutes=minute
    )
    long = direction == "long"
    close = 1_500.0 + score_bias
    payload = f"{symbol}|{observed_at.isoformat()}|{direction}|{flags}|{score_bias}"
    return {
        "symbol": symbol,
        "observed_at": observed_at,
        "available_at": observed_at + timedelta(milliseconds=250),
        "close": close,
        "bid": close - 0.05,
        "ask": close + 0.05,
        "vwap": close - 2.0 if long else close + 2.0,
        "ema_9": close + 1.0 if long else close - 1.0,
        "ema_21": close - 1.0 if long else close + 1.0,
        "rsi_14": 60.0 if long else 40.0,
        "atr_14": 3.0,
        "volume_z": 1.5,
        "return_5m_bps": 30.0 if long else -30.0,
        "market_return_5m_bps": 5.0 if long else -5.0,
        "data_quality_flags": flags or [],
        "snapshot_hash": hashlib.sha256(payload.encode()).hexdigest(),
    }


def _generate(rows: list[dict[str, object]], **overrides: object):
    kwargs = {
        "trade_symbols": ["NSE:RELIANCE-EQ", "NSE:TCS-EQ"],
        "candidate_start": time(9, 25),
        "entry_cutoff": time(14, 45),
        "cadence_minutes": 3,
        "max_candidates_per_gate": 5,
        "min_price": 100.0,
        "max_spread_bps": 15.0,
        "min_volume_z": 0.5,
        "cooldown_minutes": 15,
    }
    kwargs.update(overrides)
    return generate_baseline_candidates(pl.DataFrame(rows), **kwargs)


def test_gate_emits_frozen_long_and_short_candidates() -> None:
    result = _generate(
        [
            _snapshot(direction="long"),
            _snapshot(symbol="NSE:TCS-EQ", direction="short"),
        ]
    )

    assert result.summary() == {
        "evaluated_snapshots": 2,
        "qualified_before_ranking": 2,
        "candidate_count": 2,
        "blocked_by_quality": 0,
        "blocked_by_cooldown": 0,
    }
    assert set(result.frame["side"]) == {"LONG_CANDIDATE", "SHORT_CANDIDATE"}
    assert result.frame["candidate_version"].unique().to_list() == ["baseline_v1"]


def test_gate_blocks_critical_quality_flags_and_cooldown() -> None:
    result = _generate(
        [
            _snapshot(),
            _snapshot(minute=3),
            _snapshot(symbol="NSE:TCS-EQ", flags=["STALE_TICK"]),
        ]
    )

    assert result.frame.height == 1
    assert result.blocked_by_quality == 1
    assert result.blocked_by_cooldown == 1


def test_ranking_limit_is_deterministic_and_future_safe() -> None:
    initial = _generate(
        [
            _snapshot(symbol="NSE:RELIANCE-EQ", score_bias=0.0),
            _snapshot(symbol="NSE:TCS-EQ", score_bias=10.0),
        ],
        max_candidates_per_gate=1,
    ).frame
    extended = _generate(
        [
            _snapshot(symbol="NSE:RELIANCE-EQ", score_bias=0.0),
            _snapshot(symbol="NSE:TCS-EQ", score_bias=10.0),
            _snapshot(symbol="NSE:TCS-EQ", minute=3, direction="short"),
        ],
        max_candidates_per_gate=1,
    ).frame

    assert initial.height == 1
    cutoff = initial["created_at"].max()
    assert (
        extended.filter(pl.col("created_at") <= cutoff)["candidate_id"].to_list()
        == initial["candidate_id"].to_list()
    )


def test_candidate_artifact_is_content_addressed(tmp_path: Path) -> None:
    result = _generate([_snapshot()])

    first = write_candidate_artifact(
        project_root=tmp_path,
        session_date="2026-07-20",
        result=result,
    )
    second = write_candidate_artifact(
        project_root=tmp_path,
        session_date="2026-07-20",
        result=result,
    )

    assert first == second
    assert first.row_count == 1
    assert len(list((tmp_path / "data" / "derived" / "candidates").rglob("*.parquet"))) == 1


def test_rsi_ablation_is_explicit_and_keeps_variant_identity() -> None:
    row = _snapshot()
    row["rsi_14"] = 90.0
    kwargs = {
        "trade_symbols": ["NSE:RELIANCE-EQ"],
        "candidate_start": time(9, 25),
        "entry_cutoff": time(14, 45),
        "cadence_minutes": 3,
        "max_candidates_per_gate": 5,
        "min_price": 100.0,
        "max_spread_bps": 15.0,
        "min_volume_z": 0.5,
        "cooldown_minutes": 15,
    }

    baseline = generate_baseline_candidates(pl.DataFrame([row]), **kwargs)
    ablation = generate_rule_candidates(
        pl.DataFrame([row]),
        **kwargs,
        rule=CandidateRule(
            version="no_rsi_ablation_v1",
            long_rsi_min=None,
            long_rsi_max=None,
            short_rsi_min=None,
            short_rsi_max=None,
        ),
    )

    assert baseline.frame.is_empty()
    assert ablation.frame.height == 1
    assert ablation.frame["candidate_version"][0] == "no_rsi_ablation_v1"
    assert "rsi_filter_disabled" in ablation.frame["deterministic_reasons"][0]
