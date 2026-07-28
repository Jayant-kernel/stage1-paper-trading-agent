from datetime import date, datetime, timedelta, timezone

import polars as pl

from stage1.evaluation.universe_ranker import prepare_walk_forward_history


def _history(*, future_multiplier: float = 1.0) -> pl.DataFrame:
    symbols = ("NSE:AAA-EQ", "NSE:BBB-EQ")
    start = date(2026, 1, 1)
    rows: list[dict[str, object]] = []
    for session in range(25):
        session_date = start + timedelta(days=session)
        for symbol in symbols:
            for minute in range(7):
                base = 100.0 + session * 0.1 + minute * 0.02
                volume = 100_000.0 if symbol == symbols[0] else 1_000.0
                if session >= 15 and symbol == symbols[1]:
                    volume *= future_multiplier
                timestamp = datetime(2026, 1, 1, 3, 45, tzinfo=timezone.utc) + timedelta(
                    days=session,
                    minutes=minute,
                )
                rows.append(
                    {
                        "symbol": symbol,
                        "resolution": "60",
                        "bar_start": timestamp.isoformat(),
                        "session_date": session_date.isoformat(),
                        "open": base,
                        "high": base + 0.1,
                        "low": base - 0.1,
                        "close": base + 0.01,
                        "volume": volume,
                    }
                )
    return pl.DataFrame(rows)


def test_walk_forward_ranking_uses_training_data_only() -> None:
    normal = prepare_walk_forward_history(
        _history(),
        trade_symbols=("NSE:AAA-EQ", "NSE:BBB-EQ"),
        selected_count=1,
    )
    manipulated_future = prepare_walk_forward_history(
        _history(future_multiplier=1_000_000),
        trade_symbols=("NSE:AAA-EQ", "NSE:BBB-EQ"),
        selected_count=1,
    )
    assert normal.selected_symbols == ("NSE:AAA-EQ",)
    assert manipulated_future.selected_symbols == normal.selected_symbols
    assert normal.ranking.to_dicts() == manipulated_future.ranking.to_dicts()
    assert set(normal.frame["dataset_split"].unique()) == {
        "TRAIN",
        "VALIDATION",
        "TEST",
        "EMBARGO",
    }


def test_ranker_calls_selection_research_suitability_not_profit_prediction() -> None:
    result = prepare_walk_forward_history(
        _history(),
        trade_symbols=("NSE:AAA-EQ", "NSE:BBB-EQ"),
        selected_count=1,
    )
    assert "research_suitability_score" in result.ranking.columns
    assert result.ranking.filter(pl.col("selected_for_simple_baseline"))[
        "symbol"
    ].to_list() == ["NSE:AAA-EQ"]
