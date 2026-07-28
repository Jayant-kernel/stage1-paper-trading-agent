from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stage1.market.features import (  # noqa: E402
    build_market_features,
    write_feature_artifact,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build past-only deterministic market features from minute bars."
    )
    parser.add_argument("session_date", help="NSE session date in YYYY-MM-DD format")
    parser.add_argument("bar_artifact", help="Exact content-addressed minute-bar Parquet file")
    args = parser.parse_args()

    bar_path = Path(args.bar_artifact).resolve()
    if not bar_path.is_relative_to(ROOT.resolve()) or not bar_path.is_file():
        print(json.dumps({"status": "error", "reason": "invalid bar artifact path"}))
        return 2
    result = build_market_features(pl.read_parquet(bar_path))
    artifact = write_feature_artifact(
        project_root=ROOT,
        session_date=args.session_date,
        result=result,
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "session_date": args.session_date,
                "artifact": asdict(artifact),
                "summary": result.summary(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
