from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from stage1.schemas import QuarantinedMarketEvent, RawTickRecord


class TickSink(Protocol):
    def submit(self, record: RawTickRecord) -> None: ...

    def quarantine(self, event: QuarantinedMarketEvent) -> None: ...

    def close(self) -> None: ...


class MarketDataRecorder(Protocol):
    def connect(self) -> None: ...

    def wait_until_stopped(self) -> None: ...

    def close(self) -> None: ...


MessageCallback = Callable[[dict[str, Any]], None]
