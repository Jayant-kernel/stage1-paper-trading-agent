import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from stage1.adapters.yahoo_history import (
    IncompleteHistoryError,
    YahooDailyRequest,
    fetch_and_store_yahoo_daily,
    fetch_complete_yahoo_daily_history,
    normalize_yahoo_chart,
    to_yahoo_symbol,
)


ROOT = Path(__file__).resolve().parents[1]


def _payload() -> dict[str, object]:
    return json.loads(
        (ROOT / "tests" / "fixtures" / "yahoo_chart_daily_ok.json").read_text(
            encoding="utf-8"
        )
    )


class FakeYahooClient:
    def __init__(self, *, fail_symbol: str | None = None) -> None:
        self.fail_symbol = fail_symbol
        self.calls: list[str] = []

    def chart(self, request: YahooDailyRequest) -> tuple[int, dict[str, object]]:
        self.calls.append(request.symbol)
        if request.symbol == self.fail_symbol:
            raise RuntimeError("HTTP 404: Not Found")
        payload = _payload()
        payload["chart"]["result"][0]["meta"]["symbol"] = request.yahoo_symbol  # type: ignore[index]
        return 200, payload


def _request(symbol: str = "NSE:SBIN-EQ") -> YahooDailyRequest:
    return YahooDailyRequest(
        symbol=symbol,
        start=date(2026, 7, 20),
        end=date(2026, 7, 21),
    )


def test_fixed_nse_symbols_map_to_read_only_yahoo_symbols() -> None:
    assert to_yahoo_symbol("NSE:SBIN-EQ") == "SBIN.NS"
    assert to_yahoo_symbol("NSE:NIFTY50-INDEX") == "^NSEI"
    assert to_yahoo_symbol("NSE:INDIAVIX-INDEX") == "^INDIAVIX"
    with pytest.raises(ValueError, match="unsupported"):
        to_yahoo_symbol("NYSE:AAPL")


def test_yahoo_fixture_normalizes_and_preserves_provider_provenance() -> None:
    frame = normalize_yahoo_chart(_payload(), request=_request())
    assert frame.height == 2
    assert frame["symbol"].unique().to_list() == ["NSE:SBIN-EQ"]
    assert frame["provider"].unique().to_list() == ["YAHOO_PUBLIC_CHART"]
    assert frame["provider_symbol"].unique().to_list() == ["SBIN.NS"]
    assert frame["session_date"].to_list() == ["2026-07-20", "2026-07-21"]
    assert frame["close"].to_list() == [826.0, 831.0]


def test_public_daily_history_is_immutable_and_resumable(tmp_path: Path) -> None:
    first_client = FakeYahooClient()
    first = fetch_and_store_yahoo_daily(
        client=first_client,  # type: ignore[arg-type]
        project_root=tmp_path,
        request=_request(),
    )
    assert first.row_count == 2
    raw = json.loads((tmp_path / first.raw_relative_path).read_text(encoding="utf-8"))
    assert raw["http_status"] == 200
    assert raw["request"]["provider"] == "YAHOO_PUBLIC_CHART"

    second_client = FakeYahooClient(fail_symbol="NSE:SBIN-EQ")
    second = fetch_and_store_yahoo_daily(
        client=second_client,  # type: ignore[arg-type]
        project_root=tmp_path,
        request=_request(),
    )
    assert second.reused is True
    assert second.bars_sha256 == first.bars_sha256
    assert second_client.calls == []


def test_complete_history_gate_never_silently_shrinks_universe(tmp_path: Path) -> None:
    symbols = ("NSE:SBIN-EQ", "NSE:ITC-EQ")
    with pytest.raises(IncompleteHistoryError, match="NSE:ITC-EQ"):
        fetch_complete_yahoo_daily_history(
            client=FakeYahooClient(fail_symbol="NSE:ITC-EQ"),  # type: ignore[arg-type]
            project_root=tmp_path,
            symbols=symbols,
            start=date(2026, 7, 20),
            end=date(2026, 7, 21),
            minimum_rows_per_symbol=2,
        )

    result = fetch_complete_yahoo_daily_history(
        client=FakeYahooClient(),  # type: ignore[arg-type]
        project_root=tmp_path,
        symbols=symbols,
        start=date(2026, 7, 20),
        end=date(2026, 7, 21),
        minimum_rows_per_symbol=2,
    )
    assert result.provider == "YAHOO_PUBLIC_CHART"
    assert result.rows_by_symbol == {"NSE:SBIN-EQ": 2, "NSE:ITC-EQ": 2}
    assert result.frame.height == 4
    assert isinstance(result.frame, pl.DataFrame)


def test_yahoo_chart_rejects_provider_errors_and_bad_ohlc() -> None:
    with pytest.raises(RuntimeError, match="Not Found"):
        normalize_yahoo_chart(
            {"chart": {"result": None, "error": {"code": "Not Found", "description": "No data"}}},
            request=_request(),
        )
    payload = _payload()
    payload["chart"]["result"][0]["indicators"]["quote"][0]["high"][0] = 800.0  # type: ignore[index]
    with pytest.raises(ValueError, match="inconsistent OHLC"):
        normalize_yahoo_chart(payload, request=_request())
