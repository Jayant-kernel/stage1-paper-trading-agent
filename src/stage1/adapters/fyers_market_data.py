from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

from stage1.adapters.base_market_data import TickSink
from stage1.market.normalizer import (
    MarketMessageError,
    UnknownSymbolError,
    UnsupportedMarketMessage,
    normalize_fyers_message,
    quarantine_fyers_message,
)
from stage1.metrics import RecorderCounterSnapshot, RecorderCounters
from stage1.secrets import FyersDataCredentials, redact_text


class FyersConnectionStalled(RuntimeError):
    """The SDK exhausted its short retry cycle without receiving data."""


class FyersDataRecorder:
    """Data-only FYERS WebSocket boundary.

    This module imports only ``data_ws``. It intentionally exposes no order,
    position, trade, or execution client.
    """

    def __init__(
        self,
        *,
        symbols: tuple[str, ...],
        credentials: FyersDataCredentials,
        sink: TickSink,
        counters: RecorderCounters | None = None,
        counter_log_interval_seconds: float = 30.0,
        socket_close_timeout_seconds: float = 15.0,
        recovery_stall_seconds: float = 30.0,
    ) -> None:
        if not symbols:
            raise ValueError("at least one market-data symbol is required")
        if counter_log_interval_seconds <= 0:
            raise ValueError("counter log interval must be positive")
        if socket_close_timeout_seconds <= 0:
            raise ValueError("socket close timeout must be positive")
        if recovery_stall_seconds <= 0:
            raise ValueError("recovery stall timeout must be positive")
        self._symbols = symbols
        self._allowed_symbols = frozenset(symbols)
        self._credentials = credentials
        self._sink = sink
        self._counters = counters or RecorderCounters()
        self._counter_log_interval_seconds = counter_log_interval_seconds
        self._socket_close_timeout_seconds = socket_close_timeout_seconds
        self._recovery_stall_seconds = recovery_stall_seconds
        self._last_counter_log = 0.0
        self._counter_log_lock = threading.Lock()
        self._message_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._connected = False
        self._closed = False
        self._socket: Any | None = None
        self._last_error_monotonic: float | None = None
        self._last_error_reason: str | None = None

    def connect(self) -> None:
        from fyers_apiv3.FyersWebsocket import data_ws

        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("cannot reconnect a closed FYERS recorder")
            if self._connected:
                raise RuntimeError("FYERS recorder is already connected")
            self._connected = True

        access_token = self._credentials.websocket_access_token()

        def on_connect() -> None:
            assert self._socket is not None
            self._event(
                "connected",
                symbol_count=len(self._symbols),
                counters=self.counter_snapshot().as_dict(),
            )
            self._socket.subscribe(
                symbols=list(self._symbols),
                data_type="SymbolUpdate",
            )

        def on_message(message: dict[str, Any]) -> None:
            with self._message_lock:
                if self._stop_event.is_set():
                    return
                self._last_error_monotonic = None
                self._last_error_reason = None
                received_at = datetime.now(timezone.utc)
                try:
                    record = normalize_fyers_message(
                        message,
                        allowed_symbols=self._allowed_symbols,
                        received_at=received_at,
                    )
                except UnsupportedMarketMessage:
                    self._counters.control_message()
                    self._event(
                        "control_message_ignored",
                        counters=self.counter_snapshot().as_dict(),
                    )
                    return
                except UnknownSymbolError as exc:
                    self._sink.quarantine(
                        quarantine_fyers_message(
                            message,
                            received_at=received_at,
                            reason_code="UNKNOWN_SYMBOL",
                            reason=str(exc),
                        )
                    )
                    self._counters.quarantined_message()
                    self._event(
                        "message_quarantined",
                        severity="warning",
                        reason_code="UNKNOWN_SYMBOL",
                        counters=self.counter_snapshot().as_dict(),
                    )
                    return
                except MarketMessageError as exc:
                    self._sink.quarantine(
                        quarantine_fyers_message(
                            message,
                            received_at=received_at,
                            reason_code="INVALID_MESSAGE",
                            reason=str(exc),
                        )
                    )
                    self._counters.quarantined_message()
                    self._event(
                        "message_quarantined",
                        severity="warning",
                        reason_code="INVALID_MESSAGE",
                        counters=self.counter_snapshot().as_dict(),
                    )
                    return
                self._sink.submit(record)
                self._counters.received_tick()
                self._maybe_log_counters()

        def on_error(message: Any) -> None:
            safe_reason = redact_text(
                str(message),
                secret_values=self._credentials.known_secret_values(),
            )[:500]
            self._last_error_monotonic = time.monotonic()
            self._last_error_reason = safe_reason
            self._event("websocket_error", severity="error", reason=safe_reason)

        def on_close(message: Any) -> None:
            safe_reason = redact_text(
                str(message),
                secret_values=self._credentials.known_secret_values(),
            )[:500]
            self._event(
                "websocket_closed",
                severity="info" if self._stop_event.is_set() else "warning",
                reason=safe_reason,
                counters=self.counter_snapshot().as_dict(),
            )

        try:
            self._socket = data_ws.FyersDataSocket(
                access_token=access_token,
                log_path="",
                litemode=False,
                write_to_file=False,
                reconnect=True,
                on_connect=on_connect,
                on_close=on_close,
                on_error=on_error,
                on_message=on_message,
            )
            self._socket.connect()
        except BaseException:
            with self._lifecycle_lock:
                self._connected = False
            raise

    def wait_until_stopped(self) -> None:
        with self._lifecycle_lock:
            if not self._connected:
                raise RuntimeError("connect the FYERS recorder before waiting")
        while not self._stop_event.wait(timeout=0.5):
            last_error = self._last_error_monotonic
            if (
                last_error is not None
                and time.monotonic() - last_error >= self._recovery_stall_seconds
            ):
                raise FyersConnectionStalled(
                    self._last_error_reason or "FYERS data connection stalled"
                )

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._stop_event.set()
            socket = self._socket
        try:
            if socket is not None:
                self._close_socket(socket)
        finally:
            try:
                with self._message_lock:
                    self._sink.close()
            finally:
                self._event(
                    "recorder_stopped",
                    counters=self.counter_snapshot().as_dict(),
                )

    def _close_socket(self, socket: Any) -> None:
        close = getattr(socket, "close_connection", None)
        if not callable(close):
            return

        completed = threading.Event()
        failures: list[BaseException] = []

        def close_worker() -> None:
            try:
                close()
            except BaseException as exc:
                failures.append(exc)
            finally:
                completed.set()

        worker = threading.Thread(
            target=close_worker,
            name="stage1-fyers-socket-close",
            daemon=True,
        )
        worker.start()
        if not completed.wait(timeout=self._socket_close_timeout_seconds):
            self._event(
                "websocket_close_timeout",
                severity="warning",
                timeout_seconds=self._socket_close_timeout_seconds,
            )
            return
        if failures:
            safe_reason = redact_text(
                str(failures[0]),
                secret_values=self._credentials.known_secret_values(),
            )[:500]
            self._event(
                "websocket_close_error",
                severity="warning",
                reason=safe_reason,
            )

    def counter_snapshot(self) -> RecorderCounterSnapshot:
        return self._counters.snapshot()

    def _maybe_log_counters(self) -> None:
        now = time.monotonic()
        with self._counter_log_lock:
            if now - self._last_counter_log < self._counter_log_interval_seconds:
                return
            self._last_counter_log = now
        self._event(
            "recorder_counters",
            counters=self.counter_snapshot().as_dict(),
        )

    @staticmethod
    def _event(event: str, *, severity: str = "info", **fields: Any) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component": "fyers_market_data",
            "event": event,
            "severity": severity,
            **fields,
        }
        print(json.dumps(payload, sort_keys=True), flush=True)
