import hashlib
from datetime import datetime, timedelta, timezone

import polars as pl

from stage1.market.features import build_market_features

_MARKET = "NSE:NIFTY50-INDEX"
_STOCK = "NSE:RELIANCE-EQ"


def _bars(count: int, *, future_shock: bool = False) -> pl.DataFrame:
    start = datetime(2026, 7, 20, 3, 45, tzinfo=timezone.utc)
    rows: list[dict[str, object]] = []
    for symbol, base_price in ((_MARKET, 24_000.0), (_STOCK, 1_500.0)):
        cumulative = 1_000.0
        for index in range(count):
            price = base_price + (index * 0.5) + ((index % 3) * 0.1)
            if future_shock and index == count - 1:
                price *= 10
            bar_start = start + timedelta(minutes=index)
            volume = 100.0 + index
            cumulative += volume
            bar_id = f"{symbol}-{bar_start.isoformat()}"
            rows.append(
                {
                    "schema_version": "minute_bar_v1",
                    "provider": "FYERS",
                    "symbol": symbol,
                    "bar_start": bar_start.isoformat(),
                    "bar_end": (bar_start + timedelta(minutes=1)).isoformat(),
                    "first_received_ts": bar_start.isoformat(),
                    "last_received_ts": (bar_start + timedelta(seconds=59)).isoformat(),
                    "open": price - 0.1,
                    "high": price + 0.2,
                    "low": price - 0.2,
                    "close": price,
                    "bid_close": price - 0.05,
                    "ask_close": price + 0.05,
                    "volume": volume,
                    "cumulative_volume_end": cumulative,
                    "tick_count": 10,
                    "duplicate_receipts_dropped": 0,
                    "out_of_order_tick_count": 0,
                    "stale_tick_count": 0,
                    "clock_skew_tick_count": 0,
                    "missing_minutes_before": 0,
                    "quality_flags": [],
                    "bar_hash": hashlib.sha256(bar_id.encode()).hexdigest(),
                }
            )
    return pl.DataFrame(rows)


def test_features_are_built_after_past_only_warmup() -> None:
    result = build_market_features(_bars(70), warmup_bars=60)

    assert result.input_bar_count == 140
    assert result.snapshot_count == 22
    assert result.warmup_or_incomplete_count == 118
    assert result.missing_market_context_count == 0
    assert set(result.frame["symbol"].unique()) == {_MARKET, _STOCK}
    first_stock = result.frame.filter(pl.col("symbol") == _STOCK).row(0, named=True)
    assert first_stock["available_at"] >= first_stock["observed_at"]
    assert first_stock["ema_9"] > first_stock["ema_21"]
    assert 0 <= first_stock["rsi_14"] <= 100
    assert first_stock["atr_14"] > 0
    assert first_stock["market_return_5m_bps"] != 0


def test_future_row_does_not_change_existing_feature_hashes() -> None:
    original = build_market_features(_bars(70), warmup_bars=60).frame
    extended = build_market_features(
        _bars(71, future_shock=True),
        warmup_bars=60,
    ).frame
    original_hashes = original.sort(["symbol", "observed_at"])["snapshot_hash"].to_list()
    prior_extended_hashes = (
        extended.filter(pl.col("observed_at") <= original["observed_at"].max())
        .sort(["symbol", "observed_at"])["snapshot_hash"]
        .to_list()
    )

    assert prior_extended_hashes == original_hashes


def test_warmup_only_bars_return_an_empty_feature_frame() -> None:
    result = build_market_features(_bars(30), warmup_bars=60)

    assert result.input_bar_count == 60
    assert result.snapshot_count == 0
    assert result.frame.is_empty()
    assert result.warmup_or_incomplete_count == 60
