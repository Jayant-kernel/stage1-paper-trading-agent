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

from stage1.config import load_config  # noqa: E402
from stage1.market.bar_builder import (  # noqa: E402
    build_minute_bars,
    write_bar_artifact,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build deterministic one-minute bars from saved FYERS ticks."
    )
    parser.add_argument("session_date", help="NSE session date in YYYY-MM-DD format")
    args = parser.parse_args()

    config = load_config(ROOT / "config" / "stage1.yaml")
    partition = ROOT / "data" / "raw" / "ticks" / f"date={args.session_date}"
    paths = [str(path) for path in partition.rglob("*.parquet")]
    if not paths:
        print(json.dumps({"status": "error", "reason": "no raw ticks found"}))
        return 2
    columns = [
        "symbol",
        "exchange_ts",
        "received_ts",
        "ltp",
        "bid",
        "ask",
        "cumulative_volume",
        "provider_message_hash",
    ]
    ticks = pl.scan_parquet(paths, hive_partitioning=False).select(columns).collect()
    result = build_minute_bars(
        ticks,
        stale_tick_seconds=config.data.stale_tick_seconds,
    )
    artifact = write_bar_artifact(
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
    return 1 if result.rejected_bars else 0


if __name__ == "__main__":
    raise SystemExit(main())
