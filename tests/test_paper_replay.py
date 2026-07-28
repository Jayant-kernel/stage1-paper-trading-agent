import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from stage1.config import load_config
from stage1.paper.costs import calculate_intraday_charges, load_cost_table
from stage1.paper.replay import run_paper_replay

ROOT = Path(__file__).resolve().parents[1]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _frames() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    created = datetime(2026, 7, 20, 3, 55, 0, 500_000, tzinfo=timezone.utc)
    snapshot_hash = _hash("snapshot")
    candidate_id = _hash("candidate")
    candidates = pl.DataFrame(
        [
            {
                "candidate_version": "baseline_v1",
                "candidate_id": candidate_id,
                "symbol": "NSE:RELIANCE-EQ",
                "side": "LONG_CANDIDATE",
                "created_at": created,
                "valid_until": created + timedelta(minutes=3),
                "score": 0.75,
                "spread_bps": 10.0,
                "relative_return_5m_bps": 25.0,
                "snapshot_hash": snapshot_hash,
                "deterministic_reasons": ["fixture"],
            }
        ]
    )
    features = pl.DataFrame(
        [
            {
                "snapshot_hash": snapshot_hash,
                "close": 100.0,
                "atr_14": 1.0,
            }
        ]
    )

    def tick(name: str, at: datetime, ltp: float, bid: float, ask: float) -> dict[str, object]:
        return {
            "symbol": "NSE:RELIANCE-EQ",
            "received_ts": at,
            "ltp": ltp,
            "bid": bid,
            "ask": ask,
            "bid_qty": 10.0,
            "ask_qty": 10.0,
            "provider_message_hash": _hash(name),
        }

    ticks = pl.DataFrame(
        [
            tick("before", created - timedelta(milliseconds=250), 100.0, 99.9, 100.0),
            # A quote at exactly intent creation is not eligible.
            tick("same-time", created, 99.0, 98.9, 99.0),
            tick("entry", created + timedelta(seconds=1), 100.0, 99.9, 100.0),
            tick("stop", created + timedelta(minutes=1), 98.0, 97.9, 98.0),
        ]
    )
    bars = pl.DataFrame(
        [
            {
                "symbol": "NSE:RELIANCE-EQ",
                "bar_end": created - timedelta(seconds=1),
                "volume": 2_000.0,
            }
        ]
    )
    return candidates, features, ticks, bars


def test_current_intraday_cost_fixture() -> None:
    table = load_cost_table(ROOT / "config" / "cost_profiles.yaml")
    charges = calculate_intraday_charges(
        table=table,
        profile_name="zerodha",
        buy_turnover=10_000,
        sell_turnover=10_100,
    )

    assert charges.brokerage == 6.03
    assert charges.stt == 3.0
    assert charges.total == 11.1673


def test_replay_uses_next_quote_partial_fill_stop_and_pending_reward() -> None:
    candidates, features, ticks, bars = _frames()
    config = load_config(ROOT / "config" / "stage1.yaml")
    result = run_paper_replay(
        candidates=candidates,
        features=features,
        ticks=ticks,
        bars=bars,
        sectors={"NSE:RELIANCE-EQ": "Energy"},
        paper=config.paper,
        cost_table=load_cost_table(ROOT / "config" / "cost_profiles.yaml"),
        flatten_at=config.market.flatten,
        stale_tick_seconds=config.data.stale_tick_seconds,
    )

    assert result.approved_count == 1
    assert result.entry_fill_count == 1
    assert result.completed_trade_count == 1
    assert result.unresolved_position_count == 0
    fills = result.fills.sort("filled_at").to_dicts()
    assert fills[0]["provider_message_hash"] == _hash("entry")
    assert fills[0]["partial"] is True
    assert fills[0]["quantity"] == 10
    assert fills[1]["purpose"] == "STOP"
    assert set(result.rewards["review_status"]) == {"PENDING_HUMAN_REVIEW"}
    assert set(result.rewards["eligible_for_model_memory"]) == {False}
    assert set(result.rewards["label"]) == {"INCORRECT_AFTER_COSTS"}


def test_replay_is_identical_for_identical_inputs() -> None:
    candidates, features, ticks, bars = _frames()
    config = load_config(ROOT / "config" / "stage1.yaml")
    kwargs = {
        "candidates": candidates,
        "features": features,
        "ticks": ticks,
        "bars": bars,
        "sectors": {"NSE:RELIANCE-EQ": "Energy"},
        "paper": config.paper,
        "cost_table": load_cost_table(ROOT / "config" / "cost_profiles.yaml"),
        "flatten_at": config.market.flatten,
        "stale_tick_seconds": config.data.stale_tick_seconds,
    }

    first = run_paper_replay(**kwargs)
    second = run_paper_replay(**kwargs)

    assert first.summary() == second.summary()
    assert first.fills.to_dicts() == second.fills.to_dicts()
    assert first.outcomes.to_dicts() == second.outcomes.to_dicts()
    assert first.rewards.to_dicts() == second.rewards.to_dicts()
