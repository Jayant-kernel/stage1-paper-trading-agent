from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import polars as pl


_IST = ZoneInfo("Asia/Kolkata")
_SUPPORTED_RESOLUTIONS = frozenset(
    {"1", "2", "3", "5", "10", "15", "20", "30", "60", "120", "240", "D"}
)


class HistoryClient(Protocol):
    """The single data-only method used from the FYERS REST client."""

    def history(self, data: dict[str, object] | None = None) -> Any: ...


@dataclass(frozen=True)
class HistoryRequest:
    symbol: str
    resolution: str
    start: date
    end: date

    def __post_init__(self) -> None:
        normalized = "D" if self.resolution in {"Day", "1D", "D"} else self.resolution
        object.__setattr__(self, "resolution", normalized)
        if not self.symbol.startswith("NSE:"):
            raise ValueError("historical data is restricted to NSE symbols")
        if normalized not in _SUPPORTED_RESOLUTIONS:
            raise ValueError(f"unsupported historical resolution: {self.resolution}")
        if self.end < self.start:
            raise ValueError("historical end date cannot precede start date")

    @property
    def request_id(self) -> str:
        material = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def payload(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "resolution": self.resolution,
            "date_format": 1,
            "range_from": self.start.isoformat(),
            "range_to": self.end.isoformat(),
            "cont_flag": 1,
        }


@dataclass(frozen=True)
class HistoricalChunkArtifact:
    request: HistoryRequest
    raw_relative_path: str
    raw_sha256: str
    bars_relative_path: str
    bars_sha256: str
    row_count: int
    reused: bool


def date_chunks(start: date, end: date, *, days: int = 30) -> tuple[tuple[date, date], ...]:
    if days <= 0:
        raise ValueError("history chunk days must be positive")
    if end < start:
        raise ValueError("historical end date cannot precede start date")
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=days - 1))
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return tuple(chunks)


def normalize_history_response(
    response: Any,
    *,
    request: HistoryRequest,
) -> pl.DataFrame:
    if not isinstance(response, dict):
        raise ValueError("FYERS history response must be an object")
    if str(response.get("s", "")).lower() != "ok":
        message = str(response.get("message", "history request failed"))[:300]
        raise RuntimeError(f"FYERS history request failed: {message}")
    candles = response.get("candles")
    if not isinstance(candles, list):
        raise ValueError("FYERS history response has no candle list")

    rows: list[dict[str, object]] = []
    seen_timestamps: set[int] = set()
    for index, candle in enumerate(candles):
        if not isinstance(candle, (list, tuple)) or len(candle) < 6:
            raise ValueError(f"historical candle {index} is malformed")
        try:
            epoch = int(candle[0])
            open_price, high, low, close, volume = (float(value) for value in candle[1:6])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"historical candle {index} contains non-numeric values") from exc
        if epoch in seen_timestamps:
            raise ValueError(f"historical response contains duplicate timestamp {epoch}")
        seen_timestamps.add(epoch)
        if min(open_price, high, low, close) <= 0 or volume < 0:
            raise ValueError(f"historical candle {index} contains invalid price or volume")
        if low > min(open_price, close) or high < max(open_price, close) or low > high:
            raise ValueError(f"historical candle {index} has inconsistent OHLC values")

        bar_start = datetime.fromtimestamp(epoch, tz=timezone.utc)
        local = bar_start.astimezone(_IST)
        if not request.start <= local.date() <= request.end:
            raise ValueError(f"historical candle {index} is outside the requested range")
        rows.append(
            {
                "schema_version": "fyers_historical_candle_v1",
                "provider": "FYERS",
                "symbol": request.symbol,
                "resolution": request.resolution,
                "bar_start": bar_start.isoformat(),
                "session_date": local.date().isoformat(),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "source_request_id": request.request_id,
            }
        )
    rows.sort(key=lambda row: str(row["bar_start"]))
    return pl.DataFrame(rows)


def fetch_and_store_history_chunk(
    *,
    client: HistoryClient,
    project_root: str | Path,
    request: HistoryRequest,
) -> HistoricalChunkArtifact:
    root = Path(project_root).resolve()
    symbol_path = _safe_symbol(request.symbol)
    partition = (
        root
        / "data"
        / "historical"
        / "fyers"
        / f"resolution={request.resolution}"
        / f"symbol={symbol_path}"
    )
    partition.mkdir(parents=True, exist_ok=True)
    manifest_path = partition / f"request-{request.request_id[:20]}.json"
    existing = _read_verified_manifest(root, manifest_path, request)
    if existing is not None:
        return existing

    response = client.history(data=request.payload())
    frame = normalize_history_response(response, request=request)
    if frame.is_empty():
        raise RuntimeError(f"FYERS returned no historical candles for {request.symbol}")

    canonical_response = json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n"
    raw_bytes = canonical_response.encode("utf-8")
    raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    raw_path = partition / f"raw-{request.request_id[:12]}-{raw_hash[:16]}.json"
    _write_immutable(raw_path, raw_bytes)

    temporary = partition / f".bars-{request.request_id[:12]}.tmp"
    frame.write_parquet(temporary, compression="zstd", statistics=True)
    bars_hash = _sha256_file(temporary)
    bars_path = partition / f"bars-{request.request_id[:12]}-{bars_hash[:16]}.parquet"
    if bars_path.exists():
        if _sha256_file(bars_path) != bars_hash:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("refusing to replace an existing historical candle artifact")
        temporary.unlink(missing_ok=True)
    else:
        os.replace(temporary, bars_path)

    manifest = {
        "schema_version": "fyers_history_manifest_v1",
        "request": request.payload(),
        "request_id": request.request_id,
        "raw_relative_path": raw_path.relative_to(root).as_posix(),
        "raw_sha256": raw_hash,
        "bars_relative_path": bars_path.relative_to(root).as_posix(),
        "bars_sha256": bars_hash,
        "row_count": frame.height,
    }
    _write_immutable(
        manifest_path,
        (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )
    return HistoricalChunkArtifact(
        request=request,
        raw_relative_path=str(manifest["raw_relative_path"]),
        raw_sha256=raw_hash,
        bars_relative_path=str(manifest["bars_relative_path"]),
        bars_sha256=bars_hash,
        row_count=frame.height,
        reused=False,
    )


def load_historical_artifacts(
    project_root: str | Path,
    artifacts: list[HistoricalChunkArtifact] | tuple[HistoricalChunkArtifact, ...],
) -> pl.DataFrame:
    root = Path(project_root).resolve()
    frames = [pl.read_parquet(root / artifact.bars_relative_path) for artifact in artifacts]
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed").unique(
        subset=["symbol", "resolution", "bar_start"], keep="first"
    ).sort(["bar_start", "symbol"])


def _read_verified_manifest(
    root: Path,
    manifest_path: Path,
    request: HistoryRequest,
) -> HistoricalChunkArtifact | None:
    if not manifest_path.exists():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("request_id") != request.request_id or manifest.get("request") != request.payload():
        raise RuntimeError("historical manifest does not match its request")
    raw_path = (root / str(manifest["raw_relative_path"])).resolve()
    bars_path = (root / str(manifest["bars_relative_path"])).resolve()
    if not raw_path.is_relative_to(root) or not bars_path.is_relative_to(root):
        raise RuntimeError("historical manifest path escapes the project")
    if _sha256_file(raw_path) != manifest["raw_sha256"]:
        raise RuntimeError("historical raw response failed its hash check")
    if _sha256_file(bars_path) != manifest["bars_sha256"]:
        raise RuntimeError("historical candle artifact failed its hash check")
    return HistoricalChunkArtifact(
        request=request,
        raw_relative_path=str(manifest["raw_relative_path"]),
        raw_sha256=str(manifest["raw_sha256"]),
        bars_relative_path=str(manifest["bars_relative_path"]),
        bars_sha256=str(manifest["bars_sha256"]),
        row_count=int(manifest["row_count"]),
        reused=True,
    )


def _safe_symbol(symbol: str) -> str:
    return "".join(character if character.isalnum() or character in "_.-" else "_" for character in symbol)


def _write_immutable(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError(f"refusing to replace immutable file {path.name}")
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
