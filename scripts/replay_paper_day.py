from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stage1.config import load_config  # noqa: E402
from stage1.paper.costs import load_cost_table  # noqa: E402
from stage1.paper.replay import run_paper_replay, write_replay_artifacts  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay one saved day through the local PaperBroker.")
    parser.add_argument("session_date")
    parser.add_argument("bar_artifact")
    parser.add_argument("feature_artifact")
    parser.add_argument("candidate_artifact")
    args = parser.parse_args()
    paths = [Path(value).resolve() for value in (args.bar_artifact, args.feature_artifact, args.candidate_artifact)]
    if any(not path.is_relative_to(ROOT.resolve()) or not path.is_file() for path in paths):
        print(json.dumps({"status": "error", "reason": "invalid derived artifact path"}))
        return 2
    tick_files = sorted((ROOT / "data" / "raw" / "ticks" / f"date={args.session_date}").rglob("*.parquet"))
    if not tick_files:
        print(json.dumps({"status": "error", "reason": "no raw ticks for session"}))
        return 2
    with (ROOT / "config" / "universe.csv").open(newline="", encoding="utf-8") as handle:
        sectors = {row["symbol"]: row["sector"] for row in csv.DictReader(handle)}
    config = load_config(ROOT / "config" / "stage1.yaml")
    result = run_paper_replay(
        candidates=pl.read_parquet(paths[2]),
        features=pl.read_parquet(paths[1]),
        ticks=pl.read_parquet(tick_files),
        bars=pl.read_parquet(paths[0]),
        sectors=sectors,
        paper=config.paper,
        cost_table=load_cost_table(ROOT / "config" / "cost_profiles.yaml"),
        flatten_at=config.market.flatten,
        stale_tick_seconds=config.data.stale_tick_seconds,
    )
    artifacts = write_replay_artifacts(project_root=ROOT, session_date=args.session_date, result=result)
    print(json.dumps({"status": "ok", "summary": result.summary(), "artifacts": asdict(artifacts)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
