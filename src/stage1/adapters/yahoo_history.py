from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import polars as pl


_IST = ZoneInfo("Asia/Kolkata")
_INDEX_SYMBOLS = {
    "NSE:NIFTY50-INDEX": "^NSEI",
    "NSE:INDIAVIX-INDEX": "^INDIAVIX",
}


class YahooChartTransport(Protocol):
    """Small read-only transport boundary used by the public chart adapter."""

    def get(
        self,
        url: str,
        *,
        params: dict[str, object],
        headers: dict[str, str],
        timeout: float,
    ) -> Any: ...


@dataclass(frozen=True)
class YahooDailyRequest:
    symbol: str
    start: date
    end: date

    def __post_init__(self) -> None:
        if not self.symbol.startswith("NSE:"):
            raise ValueError("public historical data is restricted to NSE symbols")
        if self.end < self.start:
            raise ValueError("historical end date cannot precede start date")

    @property
    def yahoo_symbol(self) -> str:
        return to_yahoo_symbol(self.symbol)

    @property
    def request_id(self) -> str:
        material = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def payload(self) -> dict[str, object]:
        return {
            "provider": "YAHOO_PUBLIC_CHART",
            "symbol": self.symbol,
            "provider_symbol": self.yahoo_symbol,
            "resolution": "D",
            "range_from": self.start.isoformat(),
            "range_to": self.end.isoformat(),
        }


@dataclass(frozen=True)
class YahooDailyArtifact:
    request: YahooDailyRequest
    raw_relative_path: str
    raw_sha256: str
    bars_relative_path: str
    bars_sha256: str
    row_count: int
    reused: bool


@dataclass(frozen=True)
class CompleteDailyHistory:
    frame: pl.DataFrame
    artifacts: tuple[YahooDailyArtifact, ...]
    provider: str
    rows_by_symbol: dict[str, int]


class IncompleteHistoryError(RuntimeError):
    """Raised before research when a provider did not cover every symbol."""


class YahooChartClient:
    """No-auth, read-only client for Yahoo's public daily chart response."""

    def __init__(self, transport: YahooChartTransport | None = None) -> None:
        self._transport = transport or httpx.Client(follow_redirects=True)

    def chart(self, request: YahooDailyRequest) -> tuple[int, dict[str, Any]]:
        start_epoch = int(
            datetime.combine(request.start, time.min, tzinfo=timezone.utc).timestamp()
        )
        exclusive_end = request.end + timedelta(days=1)
        end_epoch = int(
            datetime.combine(exclusive_end, time.min, tzinfo=timezone.utc).timestamp()
        )
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{quote(request.yahoo_symbol, safe='')}"
        )
        response = self._transport.get(
            url,
            params={
                "period1": start_epoch,
                "period2": end_epoch,
                "interval": "1d",
                "events": "history",
                "includeAdjustedClose": "true",
            },
            headers={
                "Accept": "application/json",
                "User-Agent": "stage1-paper-agent/0.1 research-only",
            },
            timeout=30.0,
        )
        status = int(response.status_code)
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - provider response boundary
            raise RuntimeError(f"Yahoo chart HTTP {status} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Yahoo chart HTTP {status} returned a non-object response")
        if status != 200:
            description = _chart_error(payload) or "request failed"
            raise RuntimeError(f"Yahoo chart HTTP {status}: {description}")
        return status, payload


def to_yahoo_symbol(symbol: str) -> str:
    """Map the fixed Stage 1 NSE universe to Yahoo's read-only symbols."""

    normalized = symbol.strip().upper()
    if normalized in _INDEX_SYMBOLS:
        return _INDEX_SYMBOLS[normalized]
    if not normalized.startswith("NSE:") or not normalized.endswith("-EQ"):
        raise ValueError(f"unsupported Yahoo NSE symbol: {symbol}")
    ticker = normalized.removeprefix("NSE:").removesuffix("-EQ")
    if not ticker or not all(character.isalnum() or character in ".-&" for character in ticker):
        raise ValueError(f"unsafe Yahoo NSE symbol: {symbol}")
    return f"{ticker}.NS"


def normalize_yahoo_chart(
    payload: dict[str, Any],
    *,
    request: YahooDailyRequest,
) -> pl.DataFrame:
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        raise ValueError("Yahoo chart response has no chart object")
    error = _chart_error(payload)
    if error:
        raise RuntimeError(f"Yahoo chart request failed: {error}")
    results = chart.get("result")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise ValueError("Yahoo chart response must contain exactly one result")
    result = results[0]
    timestamps = result.get("timestamp")
    indicators = result.get("indicators")
    if not isinstance(timestamps, list) or not isinstance(indicators, dict):
        raise ValueError("Yahoo chart response is missing timestamps or indicators")
    quotes = indicators.get("quote")
    if not isinstance(quotes, list) or len(quotes) != 1 or not isinstance(quotes[0], dict):
        raise ValueError("Yahoo chart response has no quote series")
    series = quotes[0]
    columns = {name: series.get(name) for name in ("open", "high", "low", "close", "volume")}
    if any(not isinstance(values, list) for values in columns.values()):
        raise ValueError("Yahoo chart quote series is incomplete")
    if any(len(values) != len(timestamps) for values in columns.values()):
        raise ValueError("Yahoo chart quote arrays have inconsistent lengths")

    rows: list[dict[str, object]] = []
    seen_sessions: set[str] = set()
    for index, epoch_value in enumerate(timestamps):
        values = {name: columns[name][index] for name in columns}
        if all(value is None for value in values.values()):
            continue
        if any(values[name] is None for name in ("open", "high", "low", "close")):
            raise ValueError(f"Yahoo daily candle {index} has incomplete OHLC values")
        try:
            epoch = int(epoch_value)
            open_price = float(values["open"])
            high = float(values["high"])
            low = float(values["low"])
            close = float(values["close"])
            volume = 0.0 if values["volume"] is None else float(values["volume"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Yahoo daily candle {index} contains non-numeric values") from exc
        if min(open_price, high, low, close) <= 0 or volume < 0:
            raise ValueError(f"Yahoo daily candle {index} contains invalid price or volume")
        if low > min(open_price, close) or high < max(open_price, close) or low > high:
            raise ValueError(f"Yahoo daily candle {index} has inconsistent OHLC values")

        provider_timestamp = datetime.fromtimestamp(epoch, tz=timezone.utc)
        session_date = provider_timestamp.astimezone(_IST).date()
        if not request.start <= session_date <= request.end:
            continue
        session = session_date.isoformat()
        if session in seen_sessions:
            raise ValueError(f"Yahoo chart contains duplicate session {session}")
        seen_sessions.add(session)
        rows.append(
            {
                "schema_version": "public_historical_candle_v1",
                "provider": "YAHOO_PUBLIC_CHART",
                "symbol": request.symbol,
                "provider_symbol": request.yahoo_symbol,
                "resolution": "D",
                "bar_start": provider_timestamp.isoformat(),
                "session_date": session,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "source_request_id": request.request_id,
            }
        )
    rows.sort(key=lambda row: str(row["session_date"]))
    return pl.DataFrame(rows)


def fetch_and_store_yahoo_daily(
    *,
    client: YahooChartClient,
    project_root: str | Path,
    request: YahooDailyRequest,
) -> YahooDailyArtifact:
    root = Path(project_root).resolve()
    partition = (
        root
        / "data"
        / "historical"
        / "yahoo"
        / "resolution=D"
        / f"symbol={_safe_symbol(request.symbol)}"
    )
    partition.mkdir(parents=True, exist_ok=True)
    manifest_path = partition / f"request-{request.request_id[:20]}.json"
    existing = _read_verified_manifest(root, manifest_path, request)
    if existing is not None:
        return existing

    status, payload = client.chart(request)
    frame = normalize_yahoo_chart(payload, request=request)
    if frame.is_empty():
        raise RuntimeError(f"Yahoo returned no daily candles for {request.symbol}")

    envelope = {
        "http_status": status,
        "request": request.payload(),
        "response": payload,
    }
    raw_bytes = (json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
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
            raise RuntimeError("refusing to replace an existing public history artifact")
        temporary.unlink(missing_ok=True)
    else:
        os.replace(temporary, bars_path)

    manifest = {
        "schema_version": "yahoo_history_manifest_v1",
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
    return YahooDailyArtifact(
        request=request,
        raw_relative_path=str(manifest["raw_relative_path"]),
        raw_sha256=raw_hash,
        bars_relative_path=str(manifest["bars_relative_path"]),
        bars_sha256=bars_hash,
        row_count=frame.height,
        reused=False,
    )


def fetch_complete_yahoo_daily_history(
    *,
    client: YahooChartClient,
    project_root: str | Path,
    symbols: tuple[str, ...] | list[str],
    start: date,
    end: date,
    minimum_rows_per_symbol: int = 15,
) -> CompleteDailyHistory:
    requested = tuple(dict.fromkeys(symbols))
    if not requested:
        raise ValueError("at least one historical symbol is required")
    if minimum_rows_per_symbol <= 0:
        raise ValueError("minimum history rows must be positive")

    artifacts: list[YahooDailyArtifact] = []
    failures: dict[str, str] = {}
    for symbol in requested:
        request = YahooDailyRequest(symbol=symbol, start=start, end=end)
        try:
            artifact = fetch_and_store_yahoo_daily(
                client=client,
                project_root=project_root,
                request=request,
            )
            if artifact.row_count < minimum_rows_per_symbol:
                failures[symbol] = (
                    f"only {artifact.row_count} rows; minimum is {minimum_rows_per_symbol}"
                )
                continue
            artifacts.append(artifact)
        except Exception as exc:  # noqa: BLE001 - aggregate provider failures safely
            failures[symbol] = str(exc)[:300]
    if failures:
        details = "; ".join(f"{symbol}: {reason}" for symbol, reason in sorted(failures.items()))
        raise IncompleteHistoryError(
            "public daily history is incomplete; research was not prepared. " + details
        )

    root = Path(project_root).resolve()
    frames = [pl.read_parquet(root / artifact.bars_relative_path) for artifact in artifacts]
    frame = pl.concat(frames, how="diagonal_relaxed").unique(
        subset=["symbol", "resolution", "session_date"], keep="first"
    ).sort(["session_date", "symbol"])
    rows_by_symbol = {
        symbol: frame.filter(pl.col("symbol") == symbol).height for symbol in requested
    }
    missing = [symbol for symbol, count in rows_by_symbol.items() if count < minimum_rows_per_symbol]
    if missing:
        raise IncompleteHistoryError(
            f"public daily history failed its post-load completeness gate: {sorted(missing)}"
        )
    return CompleteDailyHistory(
        frame=frame,
        artifacts=tuple(artifacts),
        provider="YAHOO_PUBLIC_CHART",
        rows_by_symbol=rows_by_symbol,
    )


def _chart_error(payload: dict[str, Any]) -> str | None:
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        return None
    error = chart.get("error")
    if not error:
        return None
    if isinstance(error, dict):
        code = error.get("code")
        description = error.get("description")
        return f"{code}: {description}" if code else str(description or "unknown chart error")
    return str(error)


def _read_verified_manifest(
    root: Path,
    manifest_path: Path,
    request: YahooDailyRequest,
) -> YahooDailyArtifact | None:
    if not manifest_path.exists():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("request_id") != request.request_id or manifest.get("request") != request.payload():
        raise RuntimeError("public historical manifest does not match its request")
    raw_path = (root / str(manifest["raw_relative_path"])).resolve()
    bars_path = (root / str(manifest["bars_relative_path"])).resolve()
    if not raw_path.is_relative_to(root) or not bars_path.is_relative_to(root):
        raise RuntimeError("public historical manifest path escapes the project")
    if _sha256_file(raw_path) != manifest["raw_sha256"]:
        raise RuntimeError("public historical raw response failed its hash check")
    if _sha256_file(bars_path) != manifest["bars_sha256"]:
        raise RuntimeError("public historical candle artifact failed its hash check")
    return YahooDailyArtifact(
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
