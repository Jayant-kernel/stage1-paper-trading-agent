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
from stage1.strategy.candidate_gate import (  # noqa: E402
    generate_baseline_candidates,
    write_candidate_artifact,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate frozen deterministic baseline candidates."
    )
    parser.add_argument("feature_artifact", help="Exact market-feature Parquet file")
    args = parser.parse_args()

    feature_path = Path(args.feature_artifact).resolve()
    if not feature_path.is_relative_to(ROOT.resolve()) or not feature_path.is_file():
        print(json.dumps({"status": "error", "reason": "invalid feature artifact path"}))
        return 2
    config = load_config(ROOT / "config" / "stage1.yaml")
    result = generate_baseline_candidates(
        pl.read_parquet(feature_path),
        trade_symbols=config.universe.symbols,
        candidate_start=config.market.candidate_start,
        entry_cutoff=config.market.new_entry_cutoff,
        cadence_minutes=config.candidate_gate.cadence_minutes,
        max_candidates_per_gate=config.candidate_gate.max_candidates,
        min_price=config.candidate_gate.min_price,
        max_spread_bps=config.candidate_gate.max_estimated_spread_bps,
        min_volume_z=config.candidate_gate.min_volume_z,
        cooldown_minutes=config.candidate_gate.cooldown_minutes_after_decision,
    )
    artifact = None
    if not result.frame.is_empty():
        session_date = str(result.frame["created_at"].min())[:10]
        artifact = write_candidate_artifact(
            project_root=ROOT,
            session_date=session_date,
            result=result,
        )
    print(
        json.dumps(
            {
                "status": "ok",
                "summary": result.summary(),
                "artifact": artifact.__dict__ if artifact is not None else None,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
