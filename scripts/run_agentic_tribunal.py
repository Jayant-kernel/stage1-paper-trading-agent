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
from stage1.reasoning.agentic_tribunal import (  # noqa: E402
    run_agentic_tribunal,
    write_tribunal_artifact,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the deterministic bull/bear/risk tribunal on candidate artifacts."
    )
    parser.add_argument("candidate_artifact", help="Exact candidate Parquet file")
    parser.add_argument(
        "--min-consensus-score",
        type=int,
        default=55,
        help="Minimum consensus score needed to allow a paper policy.",
    )
    parser.add_argument(
        "--full-exposure-score",
        type=int,
        default=80,
        help="Consensus score needed to emit FULL instead of HALF exposure.",
    )
    args = parser.parse_args()

    candidate_path = Path(args.candidate_artifact).resolve()
    if not candidate_path.is_relative_to(ROOT.resolve()) or not candidate_path.is_file():
        print(json.dumps({"status": "error", "reason": "invalid candidate artifact path"}))
        return 2

    config = load_config(ROOT / "config" / "stage1.yaml")
    decisions = run_agentic_tribunal(
        pl.read_parquet(candidate_path),
        max_spread_bps=config.candidate_gate.max_estimated_spread_bps,
        min_consensus_score=args.min_consensus_score,
        full_exposure_score=args.full_exposure_score,
    )
    artifact = None
    if not decisions.is_empty():
        session_date = str(decisions["valid_until"].min())[:10]
        artifact = write_tribunal_artifact(
            project_root=ROOT,
            session_date=session_date,
            decisions=decisions,
        )

    summary = {
        "decision_count": decisions.height,
        "allowed_count": (
            decisions.filter(pl.col("action") != "HOLD").height
            if not decisions.is_empty()
            else 0
        ),
        "hold_count": (
            decisions.filter(pl.col("action") == "HOLD").height
            if not decisions.is_empty()
            else 0
        ),
    }
    print(
        json.dumps(
            {
                "status": "ok",
                "summary": summary,
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
