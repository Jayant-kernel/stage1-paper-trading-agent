import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from stage1.adapters.fyers_history import (
    HistoryRequest,
    date_chunks,
    fetch_and_store_history_chunk,
    load_historical_artifacts,
    normalize_history_response,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeHistoryClient:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def history(self, data: dict[str, object] | None = None) -> dict[str, object]:
        assert data is not None
        self.calls.append(data)
        return self.response


def _request() -> HistoryRequest:
    return HistoryRequest(
        symbol="NSE:RELIANCE-EQ",
        resolution="1",
        start=date(2026, 7, 21),
        end=date(2026, 7, 21),
    )


def _response() -> dict[str, object]:
    return json.loads(
        (ROOT / "tests" / "fixtures" / "fyers_history_ok.json").read_text(
            encoding="utf-8"
        )
    )


def test_exact_history_fixture_is_normalized_without_losing_raw_response(tmp_path: Path) -> None:
    client = FakeHistoryClient(_response())
    artifact = fetch_and_store_history_chunk(
        client=client,
        project_root=tmp_path,
        request=_request(),
    )
    assert artifact.row_count == 2
    assert artifact.reused is False
    frame = pl.read_parquet(tmp_path / artifact.bars_relative_path)
    assert frame["symbol"].unique().to_list() == ["NSE:RELIANCE-EQ"]
    assert frame["close"].to_list() == [100.5, 101.0]
    raw = json.loads((tmp_path / artifact.raw_relative_path).read_text(encoding="utf-8"))
    assert raw == _response()


def test_verified_history_request_is_resumable_without_second_api_call(tmp_path: Path) -> None:
    first_client = FakeHistoryClient(_response())
    first = fetch_and_store_history_chunk(
        client=first_client,
        project_root=tmp_path,
        request=_request(),
    )
    second_client = FakeHistoryClient({"s": "error", "message": "must not be called"})
    second = fetch_and_store_history_chunk(
        client=second_client,
        project_root=tmp_path,
        request=_request(),
    )
    assert first.bars_sha256 == second.bars_sha256
    assert second.reused is True
    assert second_client.calls == []
    combined = load_historical_artifacts(tmp_path, [first, second])
    assert combined.height == 2


def test_history_response_rejects_provider_errors_and_bad_ohlc() -> None:
    with pytest.raises(RuntimeError, match="request failed"):
        normalize_history_response(
            {"s": "error", "message": "denied"},
            request=_request(),
        )
    response = _response()
    response["candles"][0][2] = 90.0  # type: ignore[index]
    with pytest.raises(ValueError, match="inconsistent OHLC"):
        normalize_history_response(response, request=_request())


def test_history_date_chunking_is_contiguous_and_inclusive() -> None:
    chunks = date_chunks(date(2026, 1, 1), date(2026, 3, 5), days=30)
    assert chunks == (
        (date(2026, 1, 1), date(2026, 1, 30)),
        (date(2026, 1, 31), date(2026, 3, 1)),
        (date(2026, 3, 2), date(2026, 3, 5)),
    )
