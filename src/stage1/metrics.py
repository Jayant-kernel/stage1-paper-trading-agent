from __future__ import annotations

import threading
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RecorderCounterSnapshot:
    received_ticks: int
    control_messages: int
    quarantined_messages: int
    written_parquet_records: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class RecorderCounters:
    """Thread-safe, process-local counters for the live data recorder."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._received_ticks = 0
        self._control_messages = 0
        self._quarantined_messages = 0
        self._written_parquet_records = 0

    def received_tick(self, count: int = 1) -> None:
        self._increment("_received_ticks", count)

    def control_message(self, count: int = 1) -> None:
        self._increment("_control_messages", count)

    def quarantined_message(self, count: int = 1) -> None:
        self._increment("_quarantined_messages", count)

    def written_parquet_records(self, count: int) -> None:
        self._increment("_written_parquet_records", count)

    def snapshot(self) -> RecorderCounterSnapshot:
        with self._lock:
            return RecorderCounterSnapshot(
                received_ticks=self._received_ticks,
                control_messages=self._control_messages,
                quarantined_messages=self._quarantined_messages,
                written_parquet_records=self._written_parquet_records,
            )

    def _increment(self, attribute: str, count: int) -> None:
        if count < 0:
            raise ValueError("counter increments cannot be negative")
        with self._lock:
            setattr(self, attribute, getattr(self, attribute) + count)
