from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import polars as pl


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stage1.config import load_config  # noqa: E402
from stage1.evaluation.factor_lab import (  # noqa: E402
    audit_factor_causality,
    build_factor_panel,
    evaluate_factors_strict,
    write_factor_research,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit and evaluate the small explainable Stage 1 factor registry."
    )
    parser.add_argument(
        "--dataset",
        help="Prepared walk-forward Parquet path. Defaults to the newest prepared dataset.",
    )
    parser.add_argument("--minimum-symbols", type=int, default=5)
    parser.add_argument("--minimum-sessions", type=int, default=8)
    parser.add_argument("--alpha-t-threshold", type=float, default=2.5)
    args = parser.parse_args()

    config = load_config(ROOT / "config" / "stage1.yaml")
    if config.mode != "paper" or config.live_order_endpoints_enabled:
        raise SystemExit("Paper-only configuration check failed.")

    dataset = _dataset_path(args.dataset)
    bars = pl.read_parquet(dataset)
    required_splits = {"TRAIN", "VALIDATION", "TEST"}
    actual_splits = set(bars["dataset_split"].unique().to_list())
    if not required_splits <= actual_splits:
        raise SystemExit(
            f"Prepared history is missing required chronological splits: {sorted(required_splits - actual_splits)}"
        )
    cutoff = str(
        bars.filter(pl.col("dataset_split") == "TRAIN")["session_date"].cast(pl.String).max()
    )
    audit = audit_factor_causality(bars, cutoff_session=cutoff)
    panel = build_factor_panel(bars)
    evaluation = evaluate_factors_strict(
        panel,
        minimum_symbols_per_session=args.minimum_symbols,
        minimum_sessions_per_split=args.minimum_sessions,
        alpha_t_threshold=args.alpha_t_threshold,
    )
    artifact = write_factor_research(
        project_root=ROOT,
        evaluation=evaluation,
        audit=audit,
        source_dataset=dataset.relative_to(ROOT).as_posix(),
    )
    print(
        json.dumps(
            {
                "event": "factor_research_ready",
                "paper_only": True,
                "causality_audit_passed": audit.passed,
                "factor_count": audit.factor_count,
                "factor_rows": panel.height,
                "sealed_test_rows": evaluation.sealed_test_rows,
                "test_metrics_exposed": False,
                "live_rules_modified": False,
                "categories": evaluation.summary.group_by("category")
                .len()
                .sort("category")
                .to_dicts(),
                "report": artifact.report_relative_path,
            },
            sort_keys=True,
        )
    )
    return 0


def _dataset_path(value: str | None) -> Path:
    if value:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = ROOT / candidate
        resolved = candidate.resolve()
    else:
        candidates = list(
            (ROOT / "data" / "historical" / "prepared").glob("walk-forward-*.parquet")
        )
        if not candidates:
            raise SystemExit(
                "No prepared historical dataset exists. Run prepare_historical_research.py first."
            )
        resolved = max(candidates, key=lambda path: path.stat().st_mtime_ns).resolve()
    if not resolved.is_relative_to(ROOT) or not resolved.is_file():
        raise SystemExit("Prepared dataset must be an existing file inside the project.")
    return resolved


if __name__ == "__main__":
    raise SystemExit(main())
