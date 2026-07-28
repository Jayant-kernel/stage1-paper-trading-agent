from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

import polars as pl

from stage1.metrics import RecorderCounters
from stage1.schemas import (
    QuarantinedMarketEvent,
    QuarantineManifest,
    RawTickManifest,
    RawTickRecord,
)
from stage1.storage.operational_db import OperationalDatabase

_IST: Final = ZoneInfo("Asia/Kolkata")
_STOP: Final = object()


def _safe_partition_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol)


class ParquetTickWriter:
    def __init__(self, *, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.tick_root = self.project_root / "data" / "raw" / "ticks"

    def write_batch(self, records: list[RawTickRecord]) -> list[RawTickManifest]:
        if not records:
            return []
        grouped: dict[tuple[str, str], list[RawTickRecord]] = defaultdict(list)
        for record in records:
            session_date = record.tick.exchange_ts.astimezone(_IST).date().isoformat()
            grouped[(session_date, record.tick.symbol)].append(record)

        manifests: list[RawTickManifest] = []
        for (session_date, symbol), group in sorted(grouped.items()):
            group.sort(
                key=lambda record: (
                    record.tick.received_ts,
                    record.tick.exchange_ts,
                    record.tick.provider_message_hash,
                )
            )
            manifests.append(self._write_partition(group, session_date, symbol))
        return manifests

    def write_quarantine(
        self,
        event: QuarantinedMarketEvent,
    ) -> QuarantineManifest:
        session_date = event.received_ts.astimezone(_IST).date().isoformat()
        partition = self.project_root / "data" / "raw" / "quarantine" / f"date={session_date}"
        partition.mkdir(parents=True, exist_ok=True)
        timestamp = event.received_ts.strftime("%H%M%S_%f")
        destination = partition / f"event-{timestamp}-{event.event_id[:16]}.json"
        canonical = event.model_dump_json()
        file_bytes = (canonical + "\n").encode("utf-8")
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        if destination.exists():
            if _sha256_file(destination) != file_hash:
                raise RuntimeError("refusing to replace an existing quarantine event")
        else:
            temporary = destination.with_suffix(".tmp")
            temporary.write_bytes(file_bytes)
            os.replace(temporary, destination)
        return QuarantineManifest(
            event_id=event.event_id,
            relative_path=destination.relative_to(self.project_root).as_posix(),
            file_sha256=file_hash,
            received_ts=event.received_ts,
            reason_code=event.reason_code,
            payload_hash=event.payload_hash,
            created_at=datetime.now(timezone.utc),
        )

    def _write_partition(
        self,
        records: list[RawTickRecord],
        session_date: str,
        symbol: str,
    ) -> RawTickManifest:
        partition = (
            self.tick_root
            / f"date={session_date}"
            / f"symbol={_safe_partition_symbol(symbol)}"
        )
        partition.mkdir(parents=True, exist_ok=True)
        rows = [self._row(record) for record in records]
        batch_fingerprint = hashlib.sha256(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        first_received = records[0].tick.received_ts
        timestamp = first_received.strftime("%H%M%S_%f")
        temporary = partition / f".chunk-{timestamp}-{batch_fingerprint[:16]}.tmp"
        frame = pl.DataFrame(rows)
        frame.write_parquet(
            temporary,
            compression="zstd",
            statistics=True,
        )
        file_hash = _sha256_file(temporary)
        destination = partition / f"chunk-{timestamp}-{file_hash[:16]}.parquet"
        if destination.exists():
            if _sha256_file(destination) != file_hash:
                temporary.unlink(missing_ok=True)
                raise RuntimeError("refusing to replace an existing Parquet chunk")
            temporary.unlink(missing_ok=True)
        else:
            os.replace(temporary, destination)

        relative_path = destination.relative_to(self.project_root).as_posix()
        created_at = datetime.now(timezone.utc)
        manifest_material = {
            "relative_path": relative_path,
            "file_sha256": file_hash,
            "provider": "FYERS",
            "symbol": symbol,
            "session_date": session_date,
            "row_count": len(records),
        }
        manifest_id = hashlib.sha256(
            json.dumps(
                manifest_material,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return RawTickManifest(
            manifest_id=manifest_id,
            relative_path=relative_path,
            file_sha256=file_hash,
            symbol=symbol,
            session_date=session_date,
            row_count=len(records),
            first_exchange_ts=min(record.tick.exchange_ts for record in records),
            last_exchange_ts=max(record.tick.exchange_ts for record in records),
            first_received_ts=min(record.tick.received_ts for record in records),
            last_received_ts=max(record.tick.received_ts for record in records),
            created_at=created_at,
        )

    @staticmethod
    def _row(record: RawTickRecord) -> dict[str, object]:
        tick = record.tick
        return {
            "schema_version": record.schema_version,
            "provider": tick.provider,
            "symbol": tick.symbol,
            "exchange_ts": tick.exchange_ts.isoformat(),
            "received_ts": tick.received_ts.isoformat(),
            "ltp": tick.ltp,
            "bid": tick.bid,
            "ask": tick.ask,
            "bid_qty": tick.bid_qty,
            "ask_qty": tick.ask_qty,
            "last_traded_qty": tick.last_traded_qty,
            "cumulative_volume": tick.cumulative_volume,
            "provider_message_hash": tick.provider_message_hash,
            "raw_message_json": record.raw_message_json,
        }


class TickSpool:
    """Move callback records to append-only storage on a dedicated writer thread."""

    def __init__(
        self,
        *,
        writer: ParquetTickWriter,
        database: OperationalDatabase,
        batch_size: int,
        flush_seconds: float,
        counters: RecorderCounters | None = None,
    ) -> None:
        if batch_size <= 0 or flush_seconds <= 0:
            raise ValueError("batch size and flush interval must be positive")
        self._writer = writer
        self._database = database
        self._batch_size = batch_size
        self._flush_seconds = flush_seconds
        self._counters = counters
        self._queue: queue.Queue[RawTickRecord | QuarantinedMarketEvent | object] = queue.Queue(
            maxsize=batch_size * 4
        )
        self._failure: BaseException | None = None
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="stage1-parquet-writer",
            daemon=False,
        )
        self._database.initialize()
        self._thread.start()

    def submit(self, record: RawTickRecord) -> None:
        self._raise_if_failed()
        if self._closed:
            raise RuntimeError("tick spool is closed")
        self._queue.put(record, timeout=self._flush_seconds)
        self._raise_if_failed()

    def quarantine(self, event: QuarantinedMarketEvent) -> None:
        self._raise_if_failed()
        if self._closed:
            raise RuntimeError("tick spool is closed")
        self._queue.put(event, timeout=self._flush_seconds)
        self._raise_if_failed()

    def close(self) -> None:
        if self._closed:
            self._raise_if_failed()
            return
        self._raise_if_failed()
        self._closed = True
        self._queue.put(_STOP)
        self._thread.join(timeout=max(30.0, self._flush_seconds * 2))
        if self._thread.is_alive():
            raise RuntimeError("tick writer did not stop cleanly")
        self._raise_if_failed()

    def _run(self) -> None:
        batch: list[RawTickRecord] = []
        try:
            while True:
                try:
                    item = self._queue.get(timeout=self._flush_seconds)
                except queue.Empty:
                    item = None
                if item is _STOP:
                    self._flush(batch)
                    return
                if isinstance(item, RawTickRecord):
                    batch.append(item)
                elif isinstance(item, QuarantinedMarketEvent):
                    self._flush(batch)
                    manifest = self._writer.write_quarantine(item)
                    self._database.append_quarantine_manifest(manifest)
                if len(batch) >= self._batch_size or (item is None and batch):
                    self._flush(batch)
        except BaseException as exc:
            self._failure = exc

    def _flush(self, batch: list[RawTickRecord]) -> None:
        if not batch:
            return
        manifests = self._writer.write_batch(batch)
        for manifest in manifests:
            self._database.append_raw_tick_manifest(manifest)
        if self._counters is not None:
            self._counters.written_parquet_records(len(batch))
        batch.clear()

    def _raise_if_failed(self) -> None:
        if self._failure is not None:
            raise RuntimeError("append-only tick persistence failed") from self._failure


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
