from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stage1.adapters.fyers_market_data import (  # noqa: E402
    FyersConnectionStalled,
    FyersDataRecorder,
)
from stage1.config import load_config  # noqa: E402
from stage1.metrics import RecorderCounters  # noqa: E402
from stage1.secrets import load_secrets  # noqa: E402
from stage1.storage.operational_db import OperationalDatabase  # noqa: E402
from stage1.storage.parquet_writer import ParquetTickWriter, TickSpool  # noqa: E402


def main() -> int:
    config = load_config(ROOT / "config" / "stage1.yaml")
    secrets = load_secrets(ROOT / ".env")
    try:
        credentials = secrets.require_fyers_data_credentials()
    except RuntimeError as exc:
        print(f"Cannot start recorder: {exc}", file=sys.stderr)
        print(
            "Complete the manual FYERS authorization steps in README.md first.",
            file=sys.stderr,
        )
        return 2
    counters = RecorderCounters()
    reconnect_cycle = 0
    while True:
        spool = TickSpool(
            writer=ParquetTickWriter(project_root=ROOT),
            database=OperationalDatabase(ROOT / "state" / "stage1.sqlite3"),
            batch_size=config.data.parquet_batch_size,
            flush_seconds=config.data.parquet_flush_seconds,
            counters=counters,
        )
        recorder = FyersDataRecorder(
            symbols=config.universe.all_symbols,
            credentials=credentials,
            sink=spool,
            counters=counters,
        )
        try:
            recorder.connect()
            recorder.wait_until_stopped()
        except FyersConnectionStalled:
            delay = config.data.reconnect_backoff_seconds[
                min(reconnect_cycle, len(config.data.reconnect_backoff_seconds) - 1)
            ]
            reconnect_cycle += 1
            print(
                f"FYERS SDK retry cycle exhausted; restarting the data-only "
                f"socket in {delay} second(s).",
                flush=True,
            )
            recorder.close()
            try:
                import time

                time.sleep(delay)
            except KeyboardInterrupt:
                break
            continue
        except KeyboardInterrupt:
            print(
                "Stopping FYERS data recorder; finalizing saved records "
                "(this can take up to 15 seconds)."
            )
        finally:
            recorder.close()
        break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
