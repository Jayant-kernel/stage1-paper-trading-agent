from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from stage1.evaluation.factor_lab import (
    FactorRegistry,
    FactorSpec,
    LookaheadViolation,
    audit_factor_causality,
    build_factor_panel,
    default_factor_registry,
    evaluate_factors_strict,
    write_factor_research,
)


def _bars(*, future_shock: bool = False) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    start = date(2026, 1, 1)
    for symbol_index in range(6):
        symbol = f"NSE:S{symbol_index}-EQ"
        for session in range(45):
            split = "TRAIN" if session < 25 else "VALIDATION" if session < 38 else "TEST"
            close = 100.0 + symbol_index * 5 + session * (0.2 + symbol_index * 0.01)
            if future_shock and session >= 38:
                close *= 20
            rows.append(
                {
                    "symbol": symbol,
                    "resolution": "D",
                    "bar_start": (start + timedelta(days=session)).isoformat(),
                    "session_date": (start + timedelta(days=session)).isoformat(),
                    "dataset_split": split,
                    "open": close - 0.2,
                    "high": close + 1.0,
                    "low": close - 1.0,
                    "close": close,
                    "volume": 100_000.0 + symbol_index * 10_000 + session * 100,
                }
            )
    return pl.DataFrame(rows)


def test_default_registry_is_small_explainable_and_next_session_only() -> None:
    specs = default_factor_registry().list_specs()
    assert len(specs) == 6
    assert {spec.factor_id for spec in specs} == {
        "low_volatility_20",
        "momentum_20",
        "momentum_5",
        "range_position_20",
        "reversal_1",
        "volume_surprise_20",
    }
    assert {spec.decision_timing for spec in specs} == {"NEXT_SESSION_ONLY"}


def test_factor_panel_never_uses_target_across_split_boundary() -> None:
    panel = build_factor_panel(_bars())
    final_train = panel.filter(
        (pl.col("dataset_split") == "TRAIN")
        & (pl.col("session_date") == date(2026, 1, 25).isoformat())
    )
    assert final_train.height > 0
    assert final_train["forward_return_1"].null_count() == final_train.height
    assert panel.filter(pl.col("dataset_split") == "TEST").height > 0


def test_future_tampering_cannot_change_prior_default_factor_values() -> None:
    bars = _bars()
    audit = audit_factor_causality(
        bars,
        cutoff_session=date(2026, 2, 7).isoformat(),
    )
    assert audit.passed is True
    assert audit.factor_count == 6
    assert audit.audited_rows > 0


def test_causality_audit_detects_a_deliberately_leaky_factor() -> None:
    registry = FactorRegistry()
    registry.register(
        FactorSpec("leaky", "Deliberate test fixture", 1),
        lambda rows, _index: float(rows[-1]["close"]),
    )
    with pytest.raises(LookaheadViolation, match="future perturbation"):
        audit_factor_causality(
            _bars(),
            cutoff_session=date(2026, 2, 7).isoformat(),
            registry=registry,
        )


def _strong_panel() -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    base = date(2026, 3, 1)
    for split, start_offset in (("TRAIN", 0), ("VALIDATION", 20), ("TEST", 40)):
        for session in range(10):
            day = base + timedelta(days=start_offset + session)
            for symbol_index in range(6):
                rows.append(
                    {
                        "factor_id": "transparent_strength",
                        "symbol": f"NSE:S{symbol_index}-EQ",
                        "session_date": day.isoformat(),
                        "dataset_split": split,
                        "factor_value": float(symbol_index),
                        "forward_return_1": float(symbol_index) / 1000.0,
                        "decision_timing": "NEXT_SESSION_ONLY",
                    }
                )
    return pl.DataFrame(rows)


def test_strict_evaluation_uses_random_control_and_seals_test_metrics(tmp_path: Path) -> None:
    evaluation = evaluate_factors_strict(
        _strong_panel(),
        minimum_symbols_per_session=5,
        minimum_sessions_per_split=8,
        alpha_t_threshold=2.5,
    )
    row = evaluation.summary.row(0, named=True)
    assert row["category"] == "CONFIRMED_VALIDATION"
    assert row["random_control_seed_count"] == 5
    assert row["test_metrics_exposed"] is False
    assert row["eligible_for_live_rules"] is False
    assert evaluation.sealed_test_rows == 60

    audit = audit_factor_causality(
        _bars(),
        cutoff_session=date(2026, 2, 7).isoformat(),
    )
    artifact = write_factor_research(
        project_root=tmp_path,
        evaluation=evaluation,
        audit=audit,
        source_dataset="data/example.parquet",
    )
    report = (tmp_path / artifact.report_relative_path).read_text(encoding="utf-8")
    assert '"test_metrics_exposed": false' in report
    assert '"live_rules_modified": false' in report
