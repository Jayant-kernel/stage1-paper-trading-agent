from __future__ import annotations

import sqlite3
from pathlib import Path

from stage1.schemas import QuarantineManifest, RawTickManifest

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_tick_manifests (
    manifest_id TEXT PRIMARY KEY,
    relative_path TEXT NOT NULL UNIQUE,
    file_sha256 TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider = 'FYERS'),
    symbol TEXT NOT NULL,
    session_date TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count > 0),
    first_exchange_ts TEXT NOT NULL,
    last_exchange_ts TEXT NOT NULL,
    first_received_ts TEXT NOT NULL,
    last_received_ts TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantined_market_events (
    event_id TEXT PRIMARY KEY,
    relative_path TEXT NOT NULL UNIQUE,
    file_sha256 TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider = 'FYERS'),
    received_ts TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class OperationalDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_versions(version, applied_at)
                VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )

    def append_raw_tick_manifest(self, manifest: RawTickManifest) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO raw_tick_manifests (
                    manifest_id, relative_path, file_sha256, provider, symbol,
                    session_date, row_count, first_exchange_ts, last_exchange_ts,
                    first_received_ts, last_received_ts, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.manifest_id,
                    manifest.relative_path,
                    manifest.file_sha256,
                    manifest.provider,
                    manifest.symbol,
                    manifest.session_date,
                    manifest.row_count,
                    manifest.first_exchange_ts.isoformat(),
                    manifest.last_exchange_ts.isoformat(),
                    manifest.first_received_ts.isoformat(),
                    manifest.last_received_ts.isoformat(),
                    manifest.created_at.isoformat(),
                ),
            )

    def count_raw_tick_manifests(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM raw_tick_manifests"
            ).fetchone()
            assert row is not None
            return int(row[0])

    def append_quarantine_manifest(self, manifest: QuarantineManifest) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO quarantined_market_events (
                    event_id, relative_path, file_sha256, provider, received_ts,
                    reason_code, payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.event_id,
                    manifest.relative_path,
                    manifest.file_sha256,
                    manifest.provider,
                    manifest.received_ts.isoformat(),
                    manifest.reason_code,
                    manifest.payload_hash,
                    manifest.created_at.isoformat(),
                ),
            )

    def count_quarantined_market_events(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM quarantined_market_events"
            ).fetchone()
            assert row is not None
            return int(row[0])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection
