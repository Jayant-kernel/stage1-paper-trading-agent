from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stage1.adapters.fyers_history import (  # noqa: E402
    HistoryRequest,
    date_chunks,
    fetch_and_store_history_chunk,
    load_historical_artifacts,
)
from stage1.adapters.yahoo_history import (  # noqa: E402
    YahooChartClient,
    fetch_complete_yahoo_daily_history,
)
from stage1.config import load_config  # noqa: E402
from stage1.evaluation.universe_ranker import (  # noqa: E402
    prepare_walk_forward_history,
    write_prepared_history,
)
from stage1.secrets import load_secrets, redact_text  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch immutable FYERS candles and prepare leakage-safe Stage 1 research data."
    )
    yesterday = date.today() - timedelta(days=1)
    parser.add_argument("--from-date", default=(yesterday - timedelta(days=365)).isoformat())
    parser.add_argument("--to-date", default=yesterday.isoformat())
    parser.add_argument("--resolution", default="1")
    parser.add_argument(
        "--source",
        choices=("fyers", "yahoo", "auto"),
        default="fyers",
        help="Historical data source. Yahoo is research-only and daily-only.",
    )
    parser.add_argument("--chunk-days", type=int, default=30)
    parser.add_argument("--selected-count", type=int, default=4)
    parser.add_argument("--request-delay", type=float, default=0.25)
    parser.add_argument("--minimum-rows-per-symbol", type=int, default=15)
    args = parser.parse_args()

    start = date.fromisoformat(args.from_date)
    end = date.fromisoformat(args.to_date)
    if end >= date.today():
        raise SystemExit("Historical research must end before today to exclude incomplete live data.")
    config = load_config(ROOT / "config" / "stage1.yaml")
    if config.mode != "paper" or config.live_order_endpoints_enabled:
        raise SystemExit("Paper-only configuration check failed.")
    if args.source in {"yahoo", "auto"} and args.resolution != "D":
        raise SystemExit("Yahoo fallback is deliberately restricted to resolution D.")

    credentials = None
    bars = None
    provider = ""
    try:
        if args.source in {"fyers", "auto"}:
            try:
                secrets = load_secrets(ROOT / ".env")
                credentials = secrets.require_fyers_data_credentials()

                # This client is confined to its documented history method through the adapter protocol.
                from fyers_apiv3 import fyersModel

                sdk_log_path = ROOT / "state" / "fyers-history-sdk"
                sdk_log_path.mkdir(parents=True, exist_ok=True)
                client = fyersModel.FyersModel(
                    client_id=credentials.client_id.get_secret_value(),
                    token=credentials.access_token.get_secret_value(),
                    is_async=False,
                    log_path=str(sdk_log_path),
                    log_level="ERROR",
                )

                artifacts = []
                requests = [
                    HistoryRequest(
                        symbol=symbol,
                        resolution=args.resolution,
                        start=chunk_start,
                        end=chunk_end,
                    )
                    for symbol in config.universe.all_symbols
                    for chunk_start, chunk_end in date_chunks(
                        start, end, days=args.chunk_days
                    )
                ]
                for index, request in enumerate(requests, start=1):
                    artifact = fetch_and_store_history_chunk(
                        client=client,
                        project_root=ROOT,
                        request=request,
                    )
                    artifacts.append(artifact)
                    print(
                        json.dumps(
                            {
                                "event": "historical_chunk_ready",
                                "provider": "FYERS",
                                "request": index,
                                "request_total": len(requests),
                                "symbol": request.symbol,
                                "start": request.start.isoformat(),
                                "end": request.end.isoformat(),
                                "rows": artifact.row_count,
                                "reused": artifact.reused,
                            },
                            sort_keys=True,
                        )
                    )
                    if (
                        not artifact.reused
                        and index < len(requests)
                        and args.request_delay > 0
                    ):
                        time.sleep(args.request_delay)
                bars = load_historical_artifacts(ROOT, artifacts)
                provider = "FYERS"
            except Exception as exc:
                if args.source != "auto":
                    raise
                known = credentials.known_secret_values() if credentials is not None else ()
                print(
                    json.dumps(
                        {
                            "event": "historical_provider_fallback",
                            "from": "FYERS",
                            "to": "YAHOO_PUBLIC_CHART",
                            "reason": redact_text(str(exc), secret_values=known)[:300],
                            "resolution": "D",
                        },
                        sort_keys=True,
                    )
                )

        if bars is None:
            public = fetch_complete_yahoo_daily_history(
                client=YahooChartClient(),
                project_root=ROOT,
                symbols=tuple(config.universe.all_symbols),
                start=start,
                end=end,
                minimum_rows_per_symbol=args.minimum_rows_per_symbol,
            )
            for index, artifact in enumerate(public.artifacts, start=1):
                print(
                    json.dumps(
                        {
                            "event": "historical_chunk_ready",
                            "provider": public.provider,
                            "request": index,
                            "request_total": len(public.artifacts),
                            "symbol": artifact.request.symbol,
                            "start": artifact.request.start.isoformat(),
                            "end": artifact.request.end.isoformat(),
                            "rows": artifact.row_count,
                            "reused": artifact.reused,
                        },
                        sort_keys=True,
                    )
                )
            bars = public.frame
            provider = public.provider
    except Exception as exc:
        known = credentials.known_secret_values() if credentials is not None else ()
        safe = redact_text(str(exc), secret_values=known)
        raise SystemExit(f"Historical preparation stopped safely: {safe}") from exc

    assert bars is not None
    prepared = prepare_walk_forward_history(
        bars,
        trade_symbols=config.universe.symbols,
        selected_count=args.selected_count,
    )
    output = write_prepared_history(
        project_root=ROOT,
        start=start.isoformat(),
        end=end.isoformat(),
        resolution=str(args.resolution),
        prepared=prepared,
    )
    print(
        json.dumps(
            {
                "event": "historical_research_ready",
                "paper_only": True,
                "provider": provider,
                "rows": prepared.frame.height,
                "selected_symbols": prepared.selected_symbols,
                "training_sessions": len(prepared.split_dates["TRAIN"]),
                "validation_sessions": len(prepared.split_dates["VALIDATION"]),
                "test_sessions": len(prepared.split_dates["TEST"]),
                "dataset": output.dataset_relative_path,
                "ranking": output.ranking_relative_path,
                "report": output.report_relative_path,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
