from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stage1.config import load_config  # noqa: E402
from stage1.validation.recording import validate_recording_session  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate one immutable FYERS recording session."
    )
    parser.add_argument("session_date", help="NSE session date in YYYY-MM-DD format")
    args = parser.parse_args()

    config = load_config(ROOT / "config" / "stage1.yaml")
    start_deadline = (
        datetime.combine(datetime.today(), config.market.open) + timedelta(minutes=1)
    ).time()
    end_threshold = time(15, 30)
    report = validate_recording_session(
        project_root=ROOT,
        session_date=args.session_date,
        expected_symbols=config.universe.all_symbols,
        start_deadline_ist=start_deadline,
        end_threshold_ist=end_threshold,
        max_gap_seconds_allowed=max(30.0, config.data.stale_tick_seconds * 6),
    )
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 1 if report.status == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
