import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import polars as pl
import pytest

from stage1.market.bar_builder import BarArtifact, BarBuildResult
from stage1.market.features import FeatureArtifact, FeatureBuildResult
from stage1.strategy.candidate_gate import CandidateGateResult

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_live_paper", ROOT / "scripts" / "run_live_paper.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_live_paper_process_lock_rejects_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "paper.lock"
    first = MODULE.SessionLock(path)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            MODULE.SessionLock(path)
    finally:
        first.close()

    reopened = MODULE.SessionLock(path)
    reopened.close()


def test_previous_balances_uses_configured_defaults_without_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = MODULE.load_config(ROOT / "config" / "stage1.yaml")
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)

    balances = MODULE._previous_balances("2026-07-22", config)

    assert balances == {"shoonya": 100_000.0, "zerodha": 100_000.0}


def test_offline_finalizer_saves_no_trade_artifacts_without_fresh_feed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_date = "2026-07-23"
    raw = tmp_path / "data" / "raw" / "ticks" / f"date={session_date}" / "tick.parquet"
    raw.parent.mkdir(parents=True)
    raw.touch()
    state_path = tmp_path / "state" / f"live-paper-{session_date}.json"
    config = MODULE.load_config(ROOT / "config" / "stage1.yaml")
    bar_frame = pl.DataFrame(
        {"bar_end": ["2026-07-23T09:00:00+00:00"]}
    )
    bars = BarBuildResult(
        frame=bar_frame,
        raw_tick_count=1,
        deduplicated_tick_count=1,
        duplicate_receipts_dropped=0,
        out_of_order_tick_count=0,
        stale_tick_count=0,
        clock_skew_tick_count=0,
        missing_minutes=0,
        rejected_bars=(),
    )
    features = FeatureBuildResult(
        frame=pl.DataFrame({"snapshot_hash": ["fixture"]}),
        input_bar_count=1,
        snapshot_count=1,
        warmup_or_incomplete_count=0,
        missing_market_context_count=0,
    )
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    monkeypatch.setattr(
        MODULE.pl,
        "read_parquet",
        lambda _: pl.DataFrame(
            {"received_ts": ["2026-07-23T09:00:00+00:00"]}
        ),
    )
    monkeypatch.setattr(MODULE, "build_minute_bars", lambda *_args, **_kwargs: bars)
    monkeypatch.setattr(MODULE, "build_market_features", lambda *_args, **_kwargs: features)
    monkeypatch.setattr(
        MODULE,
        "write_bar_artifact",
        lambda **_kwargs: BarArtifact("bars.parquet", "a" * 64, 1),
    )
    monkeypatch.setattr(
        MODULE,
        "write_feature_artifact",
        lambda **_kwargs: FeatureArtifact("features.parquet", "b" * 64, 1),
    )
    monkeypatch.setattr(
        MODULE,
        "generate_baseline_candidates",
        lambda *_args, **_kwargs: CandidateGateResult(pl.DataFrame(), 0, 0, 0, 0),
    )
    monkeypatch.setattr(MODULE, "_send_alert", lambda _message: None)

    MODULE._finalize(
        session_date=session_date,
        started_at=datetime(2026, 7, 23, 3, 40, tzinfo=timezone.utc),
        config=config,
        starting_balances={"shoonya": 100_000.0, "zerodha": 100_000.0},
        state_path=state_path,
    )

    state = MODULE._read_json(state_path)
    assert state["status"] == "FINALIZED_NO_TRADES"
    assert state["summary"]["candidate_count"] == 0
    assert state["bar_artifact"]["relative_path"] == "bars.parquet"
    assert state["latest_raw_tick_received_at"] == "2026-07-23T09:00:00+00:00"
    assert "error" not in state
