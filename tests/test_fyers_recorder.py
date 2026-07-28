import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fyers_apiv3.FyersWebsocket import data_ws

from stage1.adapters.fyers_market_data import FyersConnectionStalled, FyersDataRecorder
from stage1.schemas import QuarantinedMarketEvent, RawTickRecord
from stage1.secrets import FyersDataCredentials

ROOT = Path(__file__).resolve().parents[1]


class RecordingSink:
    def __init__(self) -> None:
        self.records: list[RawTickRecord] = []
        self.quarantined: list[QuarantinedMarketEvent] = []
        self.closed = False

    def submit(self, record: RawTickRecord) -> None:
        self.records.append(record)

    def quarantine(self, event: QuarantinedMarketEvent) -> None:
        self.quarantined.append(event)

    def close(self) -> None:
        self.closed = True


class FakeDataSocket:
    latest: "FakeDataSocket"

    def __init__(self, **callbacks: Any) -> None:
        self.callbacks = callbacks
        self.subscription: tuple[list[str], str] | None = None
        self.closed = False
        self.keep_running_called = False
        FakeDataSocket.latest = self

    def connect(self) -> None:
        self.callbacks["on_connect"]()

    def subscribe(self, *, symbols: list[str], data_type: str) -> None:
        self.subscription = (symbols, data_type)

    def keep_running(self) -> None:
        self.keep_running_called = True

    def close_connection(self) -> None:
        self.closed = True


class BlockingCloseDataSocket(FakeDataSocket):
    release_close = threading.Event()

    def close_connection(self) -> None:
        self.release_close.wait()
        self.closed = True


class ErrorOnlyDataSocket(FakeDataSocket):
    def connect(self) -> None:
        self.callbacks["on_error"]("temporary DNS failure")


def _fixture() -> dict[str, object]:
    return json.loads(
        (ROOT / "tests" / "fixtures" / "fyers_symbol_update.json").read_text(
            encoding="utf-8"
        )
    )


def _control_fixture(name: str) -> dict[str, object]:
    return json.loads(
        (ROOT / "tests" / "fixtures" / name).read_text(encoding="utf-8")
    )


def _named_fixture(name: str) -> dict[str, object]:
    return json.loads(
        (ROOT / "tests" / "fixtures" / name).read_text(encoding="utf-8")
    )


def test_data_only_recorder_subscribes_normalizes_and_quarantines(
    monkeypatch,
) -> None:
    monkeypatch.setattr(data_ws, "FyersDataSocket", FakeDataSocket)
    sink = RecordingSink()
    credentials = FyersDataCredentials(
        client_id="CLIENT-100",
        access_token="data-token",
    )
    recorder = FyersDataRecorder(
        symbols=("NSE:RELIANCE-EQ",),
        credentials=credentials,
        sink=sink,
    )

    recorder.connect()
    socket = FakeDataSocket.latest
    assert socket.callbacks["access_token"] == "CLIENT-100:data-token"
    assert socket.callbacks["reconnect"] is True
    assert socket.subscription == (["NSE:RELIANCE-EQ"], "SymbolUpdate")
    assert socket.keep_running_called is False

    socket.callbacks["on_message"](_fixture())
    assert len(sink.records) == 1
    assert sink.records[0].tick.symbol == "NSE:RELIANCE-EQ"

    after_hours = _named_fixture("fyers_after_hours_zero_quotes.json")
    socket.callbacks["on_message"](after_hours)
    assert len(sink.records) == 2
    assert sink.records[1].tick.bid is None
    assert sink.records[1].tick.ask is None

    inconsistent = dict(after_hours)
    inconsistent["bid_size"] = 25
    socket.callbacks["on_message"](inconsistent)
    assert len(sink.quarantined) == 1
    assert sink.quarantined[0].reason_code == "INVALID_MESSAGE"
    inconsistent_raw = json.loads(sink.quarantined[0].raw_message_json)
    assert inconsistent_raw["bid_price"] == 0
    assert inconsistent_raw["bid_size"] == 25

    socket.callbacks["on_message"](
        _control_fixture("fyers_control_authentication_done.json")
    )
    socket.callbacks["on_message"](
        _control_fixture("fyers_control_full_mode_on.json")
    )
    assert len(sink.quarantined) == 1

    invalid = _fixture()
    invalid["symbol"] = "NSE:UNKNOWN-EQ"
    invalid["access_token"] = "must-be-redacted"
    socket.callbacks["on_message"](invalid)
    assert len(sink.quarantined) == 2
    assert sink.quarantined[1].reason_code == "UNKNOWN_SYMBOL"
    assert "must-be-redacted" not in sink.quarantined[1].raw_message_json

    counters = recorder.counter_snapshot()
    assert counters.received_ticks == 2
    assert counters.control_messages == 2
    assert counters.quarantined_messages == 2
    assert counters.written_parquet_records == 0

    waiter = threading.Thread(target=recorder.wait_until_stopped)
    waiter.start()
    waiter.join(timeout=0.05)
    assert waiter.is_alive()

    recorder.close()
    waiter.join(timeout=1)
    assert not waiter.is_alive()
    assert socket.closed is True
    assert sink.closed is True


def test_recorder_bounds_blocked_sdk_close_and_still_closes_sink(
    monkeypatch,
) -> None:
    BlockingCloseDataSocket.release_close.clear()
    monkeypatch.setattr(data_ws, "FyersDataSocket", BlockingCloseDataSocket)
    sink = RecordingSink()
    recorder = FyersDataRecorder(
        symbols=("NSE:RELIANCE-EQ",),
        credentials=FyersDataCredentials(
            client_id="CLIENT-100",
            access_token="data-token",
        ),
        sink=sink,
        socket_close_timeout_seconds=0.05,
    )
    recorder.connect()

    started = time.monotonic()
    recorder.close()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert sink.closed is True
    BlockingCloseDataSocket.release_close.set()


def test_messages_are_ignored_after_shutdown_starts(monkeypatch) -> None:
    monkeypatch.setattr(data_ws, "FyersDataSocket", FakeDataSocket)
    sink = RecordingSink()
    recorder = FyersDataRecorder(
        symbols=("NSE:RELIANCE-EQ",),
        credentials=FyersDataCredentials(
            client_id="CLIENT-100",
            access_token="data-token",
        ),
        sink=sink,
    )
    recorder.connect()
    socket = FakeDataSocket.latest
    recorder.close()

    socket.callbacks["on_message"](_fixture())

    assert sink.records == []
    assert recorder.counter_snapshot().received_ticks == 0


def test_exhausted_sdk_retry_cycle_returns_control_to_outer_supervisor(
    monkeypatch,
) -> None:
    monkeypatch.setattr(data_ws, "FyersDataSocket", ErrorOnlyDataSocket)
    sink = RecordingSink()
    recorder = FyersDataRecorder(
        symbols=("NSE:RELIANCE-EQ",),
        credentials=FyersDataCredentials(
            client_id="CLIENT-100",
            access_token="data-token",
        ),
        sink=sink,
        recovery_stall_seconds=0.05,
    )
    recorder.connect()

    with pytest.raises(FyersConnectionStalled, match="temporary DNS failure"):
        recorder.wait_until_stopped()

    recorder.close()
    assert sink.closed is True
