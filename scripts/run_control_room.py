"""Read-only local API for the Stage 1 paper-trading control room.

This process exposes only whitelisted operational fields. It never reads
credentials, accepts commands, or imports any broker order functionality.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import threading
import time
from collections import Counter
from datetime import date, datetime, time as clock_time, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import polars as pl
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = PROJECT_ROOT / "state"
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = PROJECT_ROOT / "config" / "stage1.yaml"
IST = ZoneInfo("Asia/Kolkata")
SESSION_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ALLOWED_ORIGINS = {
    "http://127.0.0.1:3000",
    "http://localhost:3000",
}

_cache_lock = threading.Lock()
_snapshot_cache: dict[str, tuple[float, dict]] = {}


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _round(value: float | int | None, digits: int = 2) -> float | None:
    return round(float(value), digits) if value is not None else None


def _load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _state_paths() -> list[Path]:
    return sorted(STATE_DIR.glob("live-paper-*.json"), reverse=True)


def _session_date_from_path(path: Path) -> str:
    return path.stem.removeprefix("live-paper-")


def _select_state(session_date: str | None) -> tuple[Path, dict]:
    paths = _state_paths()
    if not paths:
        raise FileNotFoundError("No live paper session state exists yet.")
    if session_date:
        if not SESSION_PATTERN.match(session_date):
            raise ValueError("Invalid session date.")
        target = STATE_DIR / f"live-paper-{session_date}.json"
        if not target.exists():
            raise FileNotFoundError(f"No session exists for {session_date}.")
        return target, _read_json(target)
    return paths[0], _read_json(paths[0])


def _market_phase(session_date: str, config: dict, now: datetime) -> dict:
    current_ist = now.astimezone(IST)
    session_day = date.fromisoformat(session_date)
    market_cfg = config["market"]

    def at(name: str) -> datetime:
        parsed = clock_time.fromisoformat(market_cfg[name])
        return datetime.combine(session_day, parsed, IST)

    open_at = at("open")
    cutoff_at = at("new_entry_cutoff")
    flatten_at = at("flatten")
    shutdown_at = at("shutdown")
    is_today = session_day == current_ist.date()
    is_weekday = session_day.weekday() < 5

    if not is_today:
        phase = "HISTORICAL"
    elif not is_weekday:
        phase = "MARKET_CLOSED"
    elif current_ist < open_at:
        phase = "PRE_OPEN"
    elif current_ist < cutoff_at:
        phase = "ENTRY_WINDOW"
    elif current_ist < flatten_at:
        phase = "MANAGE_ONLY"
    elif current_ist < shutdown_at:
        phase = "FINALIZING"
    else:
        phase = "MARKET_CLOSED"

    return {
        "phase": phase,
        "isLiveWindow": phase in {"PRE_OPEN", "ENTRY_WINDOW", "MANAGE_ONLY", "FINALIZING"},
        "nowIst": current_ist.isoformat(),
        "openAt": open_at.isoformat(),
        "entryCutoffAt": cutoff_at.isoformat(),
        "flattenAt": flatten_at.isoformat(),
        "shutdownAt": shutdown_at.isoformat(),
    }


def _safe_event(event: dict) -> dict:
    event_type = str(event.get("event_type", "unknown"))
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    common = {
        "candidate_id",
        "symbol",
        "side",
        "candidate_side",
        "status",
        "action",
        "purpose",
        "quantity",
        "requested_quantity",
        "fill_price",
        "entry_estimate",
        "entry_limit",
        "stop_trigger",
        "entry_price",
        "exit_price",
        "gross_pnl",
        "net_pnl",
        "profile",
        "cost_profile",
        "exit_reason",
        "score",
        "spread_bps",
        "relative_return_5m_bps",
        "reasons",
        "deterministic_reasons",
        "reason",
        "label",
        "review_status",
        "reward_r_multiple",
        "bonus_points",
        "created_at",
        "decided_at",
        "filled_at",
        "entry_at",
        "exit_at",
        "valid_until",
    }
    safe_payload = {key: payload[key] for key in common if key in payload}
    return {
        "id": str(event.get("event_id", ""))[:16],
        "type": event_type,
        "recordedAt": event.get("recorded_at"),
        "payload": safe_payload,
    }


def _load_events(session_date: str) -> list[dict]:
    path = DATA_DIR / "decisions" / f"date={session_date}" / "live-paper-events.jsonl"
    if not path.exists():
        return []
    events: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def _build_trades(events: list[dict]) -> list[dict]:
    entries: dict[str, dict] = {}
    exits: dict[str, dict] = {}
    outcomes: dict[str, dict[str, dict]] = {}
    for event in events:
        payload = event.get("payload", {})
        candidate_id = payload.get("candidate_id")
        if not candidate_id:
            continue
        if event.get("event_type") == "fills":
            if payload.get("purpose") == "ENTRY":
                entries[candidate_id] = payload
            else:
                exits[candidate_id] = payload
        elif event.get("event_type") == "outcomes":
            profile = payload.get("profile") or payload.get("cost_profile")
            if profile:
                outcomes.setdefault(candidate_id, {})[str(profile)] = payload

    trades: list[dict] = []
    for candidate_id, entry in entries.items():
        exit_fill = exits.get(candidate_id, {})
        profile_outcomes = outcomes.get(candidate_id, {})
        reference = next(iter(profile_outcomes.values()), {})
        trades.append(
            {
                "candidateId": candidate_id[:12],
                "symbol": entry.get("symbol"),
                "direction": reference.get("direction")
                or ("LONG" if entry.get("action") == "BUY" else "SHORT"),
                "quantity": entry.get("quantity"),
                "entryPrice": _round(entry.get("fill_price"), 4),
                "entryAt": entry.get("filled_at"),
                "exitPrice": _round(exit_fill.get("fill_price"), 4),
                "exitAt": exit_fill.get("filled_at"),
                "exitReason": reference.get("exit_reason"),
                "status": "CLOSED" if exit_fill else "OPEN",
                "pnl": {
                    profile: _round(outcome.get("net_pnl"), 2)
                    for profile, outcome in profile_outcomes.items()
                },
            }
        )
    return sorted(trades, key=lambda item: item.get("entryAt") or "", reverse=True)


def _build_live_target(
    events: list[dict],
    *,
    market_live: bool,
    recorder_status: str,
    engine_status: str,
) -> dict:
    """Build a safe, read-only view of the latest deterministic paper target."""

    candidates: dict[str, tuple[str, dict]] = {}
    decisions: dict[str, tuple[str, dict]] = {}
    intents: dict[str, tuple[str, dict]] = {}
    entries: dict[str, tuple[str, dict]] = {}
    exits: dict[str, tuple[str, dict]] = {}
    rejections: dict[str, tuple[str, dict]] = {}

    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        candidate_id = str(payload.get("candidate_id") or "")
        if not candidate_id:
            continue
        recorded_at = str(event.get("recorded_at") or "")
        event_type = event.get("event_type")
        if event_type == "candidates":
            candidates[candidate_id] = (recorded_at, payload)
        elif event_type == "risk_decisions":
            decisions[candidate_id] = (recorded_at, payload)
        elif event_type == "intents":
            intents[candidate_id] = (recorded_at, payload)
        elif event_type == "fills":
            if payload.get("purpose") == "ENTRY":
                entries[candidate_id] = (recorded_at, payload)
            else:
                exits[candidate_id] = (recorded_at, payload)
        elif event_type == "rejections":
            rejections[candidate_id] = (recorded_at, payload)

    open_candidate_ids = [candidate_id for candidate_id in entries if candidate_id not in exits]
    if open_candidate_ids:
        candidate_id = max(open_candidate_ids, key=lambda key: entries[key][0])
    elif candidates:
        candidate_id = max(candidates, key=lambda key: candidates[key][0])
    else:
        feed_healthy = recorder_status == "LIVE" and engine_status == "LIVE"
        if not market_live:
            mode = "SESSION_IDLE"
            explanation = "The selected market session is not accepting new paper targets."
        elif not feed_healthy:
            mode = "FEED_HOLD"
            explanation = (
                "No target is permitted while the recorder or paper engine is not current."
            )
        else:
            mode = "SCANNING"
            explanation = (
                "The frozen baseline is scanning all 10 eligible stocks; no reliable "
                "candidate has passed the deterministic gate yet."
            )
        return {
            "mode": mode,
            "candidateId": None,
            "symbol": None,
            "direction": None,
            "score": None,
            "candidateAt": None,
            "riskStatus": "NOT_EVALUATED",
            "quantity": 0,
            "referencePrice": None,
            "stopPrice": None,
            "simulatedExposure": 0.0,
            "riskAtStop": 0.0,
            "fillStatus": "NO_FILL",
            "reasons": [],
            "explanation": explanation,
        }

    candidate = candidates.get(candidate_id, ("", {}))[1]
    decision = decisions.get(candidate_id, ("", {}))[1]
    intent = intents.get(candidate_id, ("", {}))[1]
    entry = entries.get(candidate_id, ("", {}))[1]
    exit_fill = exits.get(candidate_id, ("", {}))[1]
    rejection = rejections.get(candidate_id, ("", {}))[1]

    side = (
        candidate.get("side")
        or decision.get("candidate_side")
        or ("LONG_CANDIDATE" if entry.get("action") == "BUY" else "SHORT_CANDIDATE")
    )
    direction = "LONG" if side == "LONG_CANDIDATE" else "SHORT"
    quantity = int(
        entry.get("quantity")
        or intent.get("quantity")
        or decision.get("quantity")
        or 0
    )
    reference_price = (
        entry.get("fill_price")
        or intent.get("entry_limit")
        or decision.get("entry_estimate")
    )
    stop_price = intent.get("stop_trigger") or decision.get("stop_trigger")
    risk_status = str(decision.get("status") or "EVALUATING")
    reasons = decision.get("reasons") or candidate.get("deterministic_reasons") or []

    if entry and not exit_fill:
        mode = "POSITION_OPEN"
        fill_status = "SIMULATED_POSITION_OPEN"
        explanation = "A simulated position is open and is being managed by its paper stop and exit rules."
    elif entry and exit_fill:
        mode = "TRADE_CLOSED"
        fill_status = "SIMULATED_TRADE_CLOSED"
        explanation = "The most recent simulated target has been exited."
    elif rejection:
        mode = "FILL_REJECTED"
        fill_status = "REJECTED"
        reasons = list(reasons) + [str(rejection.get("reason") or "paper_fill_rejected")]
        explanation = "The candidate passed risk but did not receive a valid next-observable paper fill."
    elif risk_status == "HOLD":
        mode = "RISK_HOLD"
        fill_status = "NO_FILL"
        explanation = "The deterministic risk gate blocked this candidate; no paper money was allocated."
    elif intent:
        mode = "AWAITING_FILL"
        fill_status = "WAITING_FOR_NEXT_OBSERVABLE_TICK"
        explanation = "The candidate is approved and waiting for a valid next-observable simulated fill."
    else:
        mode = "EVALUATING"
        fill_status = "NO_FILL"
        explanation = "A deterministic candidate exists and is being evaluated by the paper risk gate."

    exposure = (
        float(quantity) * float(reference_price)
        if quantity and reference_price is not None
        else 0.0
    )
    risk_at_stop = (
        abs(float(reference_price) - float(stop_price)) * float(quantity)
        if quantity and reference_price is not None and stop_price is not None
        else 0.0
    )
    return {
        "mode": mode,
        "candidateId": candidate_id[:12],
        "symbol": candidate.get("symbol") or decision.get("symbol") or entry.get("symbol"),
        "direction": direction,
        "score": _round(candidate.get("score"), 4),
        "candidateAt": candidate.get("created_at"),
        "riskStatus": risk_status,
        "quantity": quantity,
        "referencePrice": _round(reference_price, 4),
        "stopPrice": _round(stop_price, 4),
        "simulatedExposure": _round(exposure, 2),
        "riskAtStop": _round(risk_at_stop, 2),
        "fillStatus": fill_status,
        "reasons": [str(reason) for reason in reasons],
        "explanation": explanation,
    }


def _latest_tick_for_symbol(
    session_date: str,
    symbol: str,
    now: datetime,
    live_window: bool,
    stale_seconds: float,
) -> dict:
    folder_name = symbol.replace(":", "_")
    directory = DATA_DIR / "raw" / "ticks" / f"date={session_date}" / f"symbol={folder_name}"
    files = list(directory.glob("*.parquet"))
    if not files:
        return {
            "symbol": symbol,
            "shortName": symbol.split(":", 1)[-1].replace("-EQ", "").replace("-INDEX", ""),
            "status": "NO_DATA",
            "ltp": None,
            "bid": None,
            "ask": None,
            "exchangeTs": None,
            "receivedTs": None,
            "tickAgeSec": None,
            "latencyMs": None,
            "latencyP50Ms": None,
            "files": 0,
        }

    latest_file = max(files, key=lambda path: path.stat().st_mtime_ns)
    try:
        frame = pl.read_parquet(
            latest_file,
            columns=["exchange_ts", "received_ts", "ltp", "bid", "ask"],
        )
    except Exception:
        frame = pl.read_parquet(latest_file)
    rows = frame.tail(500).to_dicts()
    latest = max(rows, key=lambda row: str(row.get("received_ts") or "")) if rows else {}
    exchange_ts = _parse_timestamp(latest.get("exchange_ts"))
    received_ts = _parse_timestamp(latest.get("received_ts"))
    latencies = []
    for row in rows:
        exchange = _parse_timestamp(row.get("exchange_ts"))
        received = _parse_timestamp(row.get("received_ts"))
        if exchange and received:
            latency = (received - exchange).total_seconds() * 1000
            if -1000 <= latency <= 300_000:
                latencies.append(latency)
    tick_age = (now - received_ts).total_seconds() if received_ts else None
    if live_window:
        status = "FRESH" if tick_age is not None and tick_age <= stale_seconds else "STALE"
    else:
        status = "CLOSED"
    return {
        "symbol": symbol,
        "shortName": symbol.split(":", 1)[-1].replace("-EQ", "").replace("-INDEX", ""),
        "status": status,
        "ltp": _round(latest.get("ltp"), 2),
        "bid": _round(latest.get("bid"), 2),
        "ask": _round(latest.get("ask"), 2),
        "exchangeTs": _iso(exchange_ts),
        "receivedTs": _iso(received_ts),
        "tickAgeSec": _round(max(tick_age, 0), 2) if tick_age is not None else None,
        "latencyMs": _round(
            (received_ts - exchange_ts).total_seconds() * 1000
            if received_ts and exchange_ts
            else None,
            2,
        ),
        "latencyP50Ms": _round(median(latencies), 2) if latencies else None,
        "files": len(files),
    }


def _manifest_quality(session_date: str) -> dict:
    database = STATE_DIR / "stage1.sqlite3"
    if not database.exists():
        return {"manifestFiles": 0, "manifestRows": 0, "quarantined": 0}
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        manifest_files, manifest_rows = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(row_count), 0) "
            "FROM raw_tick_manifests WHERE session_date = ?",
            (session_date,),
        ).fetchone()
        quarantined = connection.execute(
            "SELECT COUNT(*) FROM quarantined_market_events "
            "WHERE substr(received_ts, 1, 10) = ?",
            (session_date,),
        ).fetchone()[0]
        return {
            "manifestFiles": int(manifest_files),
            "manifestRows": int(manifest_rows),
            "quarantined": int(quarantined),
        }
    finally:
        connection.close()


def _session_history() -> list[dict]:
    sessions = []
    for path in _state_paths():
        try:
            state = _read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        summary = state.get("summary", {})
        starting = state.get("starting_equity_by_profile", {})
        ending = summary.get("ending_equity_by_profile", {})
        sessions.append(
            {
                "date": state.get("session_date") or _session_date_from_path(path),
                "status": state.get("status", "UNKNOWN"),
                "trades": summary.get("completed_trade_count", 0),
                "shoonyaPnl": _round(
                    ending.get("shoonya", 0) - starting.get("shoonya", 0), 2
                )
                if ending and starting
                else None,
                "zerodhaPnl": _round(
                    ending.get("zerodha", 0) - starting.get("zerodha", 0), 2
                )
                if ending and starting
                else None,
            }
        )
    return sessions


def build_snapshot(session_date: str | None = None) -> dict:
    cache_key = session_date or "latest"
    with _cache_lock:
        cached = _snapshot_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 1.5:
            return cached[1]

    state_path, state = _select_state(session_date)
    selected_date = str(state.get("session_date") or _session_date_from_path(state_path))
    config = _load_config()
    now = datetime.now(timezone.utc)
    market = _market_phase(selected_date, config, now)
    tradable_symbols = list(config["universe"]["symbols"])
    context_symbols = list(config["universe"]["context_symbols"])
    active_symbols = list(
        state.get("active_symbols") or tradable_symbols + context_symbols
    )
    risk_config = config["paper"]
    stale_seconds = float(config["data"]["stale_tick_seconds"])
    symbols = [
        _latest_tick_for_symbol(
            selected_date, symbol, now, market["isLiveWindow"], stale_seconds
        )
        for symbol in active_symbols
    ]
    events = _load_events(selected_date)
    event_counts = Counter(event.get("event_type", "unknown") for event in events)
    summary = state.get("summary", {})
    starting = state.get("starting_equity_by_profile", {})
    ending = summary.get("ending_equity_by_profile", {})
    quality = _manifest_quality(selected_date)
    latest_received = max(
        (_parse_timestamp(symbol.get("receivedTs")) for symbol in symbols),
        default=None,
        key=lambda value: value or datetime.min.replace(tzinfo=timezone.utc),
    )
    heartbeat = _parse_timestamp(state.get("heartbeat_at"))
    heartbeat_age = (now - heartbeat).total_seconds() if heartbeat else None
    raw_age = (now - latest_received).total_seconds() if latest_received else None
    # Recorder files are flushed in 500-row batches (roughly every 30 seconds).
    # Component health therefore uses a 45-second heartbeat window, while the
    # per-symbol decision freshness shown below remains the strict configured
    # six-second threshold.
    recorder_status = (
        "LIVE"
        if market["isLiveWindow"] and raw_age is not None and raw_age <= 45
        else "INTERRUPTED"
        if market["isLiveWindow"]
        else "SESSION_CLOSED"
    )
    engine_status = (
        "LIVE"
        if market["isLiveWindow"] and heartbeat_age is not None and heartbeat_age <= 45
        else "WAITING"
        if market["isLiveWindow"]
        else state.get("status", "OFFLINE")
    )
    trades = _build_trades(events)
    target = _build_live_target(
        events,
        market_live=market["isLiveWindow"],
        recorder_status=recorder_status,
        engine_status=engine_status,
    )
    primary_equity = next(
        (
            float(ending.get(profile, value))
            for profile, value in starting.items()
            if value is not None
        ),
        float(risk_config["starting_cash"]),
    )

    complete_bars = int(summary.get("complete_bars", 0) or 0)
    feature_snapshots = int(summary.get("feature_snapshots", 0) or 0)
    warmup_bars_per_symbol = 60
    symbol_count = max(len(active_symbols), 1)
    warmup_total_bars = warmup_bars_per_symbol * symbol_count
    estimated_bars_per_symbol = min(
        complete_bars // symbol_count,
        warmup_bars_per_symbol,
    )
    estimated_warmup_minutes = max(
        warmup_bars_per_symbol - estimated_bars_per_symbol,
        0,
    )
    warmup_active = (
        state.get("status") == "WARMING_UP" and feature_snapshots == 0
    )
    if warmup_active and target.get("candidateId") is None:
        target.update(
            {
                "mode": "WARMING_UP",
                "riskStatus": "NOT_EVALUATED",
                "fillStatus": "NO_FILL",
                "explanation": (
                    "The feed and paper engine are healthy, but the frozen "
                    f"feature pipeline needs {warmup_bars_per_symbol} complete "
                    "one-minute bars per symbol before candidate evaluation."
                ),
                "reasons": [
                    f"Estimated warm-up progress: {estimated_bars_per_symbol}"
                    f"/{warmup_bars_per_symbol} bars per symbol."
                ],
            }
        )

    snapshot = {
        "generatedAt": now.isoformat(),
        "session": {
            "date": selected_date,
            "status": state.get("status", "UNKNOWN"),
            "startedAt": state.get("started_at"),
            "finishedAt": state.get("finished_at"),
            "heartbeatAt": state.get("heartbeat_at"),
            "heartbeatAgeSec": _round(max(heartbeat_age, 0), 1)
            if heartbeat_age is not None
            else None,
            "baselineVersion": state.get("baseline_version", "unknown"),
            "runCard": state.get("run_card"),
            "warmup": {
                "active": warmup_active,
                "completeBars": complete_bars,
                "featureSnapshots": feature_snapshots,
                "requiredBarsPerSymbol": warmup_bars_per_symbol,
                "estimatedBarsPerSymbol": estimated_bars_per_symbol,
                "estimatedMinutesRemaining": estimated_warmup_minutes,
                "progressPct": _round(
                    min(complete_bars / warmup_total_bars, 1) * 100,
                    1,
                ),
            },
        },
        "market": market,
        "universe": {
            "tradableSymbols": tradable_symbols,
            "contextSymbols": context_symbols,
            "tradableCount": len(tradable_symbols),
            "contextCount": len(context_symbols),
            "totalCount": len(active_symbols),
        },
        "limits": {
            "startingCash": _round(risk_config["starting_cash"], 2),
            "maxOpenPositions": int(risk_config["max_open_positions"]),
            "maxRoundTripsPerDay": int(risk_config["max_round_trips_per_day"]),
            "riskPerTradeFraction": float(risk_config["risk_per_trade_fraction"]),
            "maxPositionFraction": float(risk_config["max_position_fraction"]),
            "currentRiskBudget": _round(
                primary_equity * float(risk_config["risk_per_trade_fraction"]), 2
            ),
            "currentMaxExposure": _round(
                primary_equity * float(risk_config["max_position_fraction"]), 2
            ),
        },
        "components": {
            "recorder": recorder_status,
            "paperEngine": engine_status,
            "fyers": "CONNECTED_INFERRED"
            if recorder_status == "LIVE"
            else "SESSION_CLOSED"
            if not market["isLiveWindow"]
            else "DISCONNECTED",
            "bridge": "LIVE",
        },
        "safety": {
            "paperOnly": bool(state.get("paper_only", config.get("mode") == "paper")),
            "liveOrderEndpointsEnabled": bool(
                state.get(
                    "live_order_endpoints_enabled",
                    config.get("live_order_endpoints_enabled", False),
                )
            ),
            "brokerOrdersPossible": False,
            "qwenEnabled": False,
            "cloudModelsEnabled": False,
            "newsDecisionsEnabled": False,
            "automaticLearningEnabled": False,
            "outboundAlertsOnly": bool(config.get("alerts", {}).get("outbound_only", True)),
        },
        "metrics": {
            "rawTickFiles": state.get("raw_tick_files") or quality["manifestFiles"],
            "manifestRows": quality["manifestRows"],
            "quarantined": quality["quarantined"],
            "candidates": summary.get("candidate_count", event_counts["candidates"]),
            "approved": summary.get("approved_count", 0),
            "held": summary.get("hold_count", 0),
            "entryFills": summary.get("entry_fill_count", 0),
            "completedTrades": summary.get("completed_trade_count", 0),
            "openPositions": summary.get("unresolved_position_count", 0),
            "latestTickAgeSec": _round(max(raw_age, 0), 2)
            if raw_age is not None and market["isLiveWindow"]
            else None,
            "freshSymbols": sum(symbol["status"] == "FRESH" for symbol in symbols),
            "staleSymbols": sum(symbol["status"] == "STALE" for symbol in symbols),
        },
        "ledgers": [
            {
                "profile": profile,
                "starting": _round(value, 2),
                "ending": _round(ending.get(profile), 2) if ending.get(profile) is not None else None,
                "pnl": _round(ending.get(profile) - value, 2)
                if ending.get(profile) is not None
                else None,
            }
            for profile, value in starting.items()
        ],
        "target": target,
        "pipeline": [
            {"name": "FYERS feed", "status": recorder_status, "count": quality["manifestRows"]},
            {"name": "Normalize", "status": "READY", "count": quality["manifestRows"]},
            {
                "name": "1m bars",
                "status": "WARMING_UP" if warmup_active else "READY",
                "count": complete_bars,
            },
            {
                "name": "Features",
                "status": "WARMING_UP" if warmup_active else "READY",
                "count": feature_snapshots,
            },
            {
                "name": "Candidates",
                "status": "WAITING" if warmup_active else "READY",
                "count": summary.get("candidate_count", event_counts["candidates"]),
            },
            {
                "name": "Risk gate",
                "status": "WAITING" if warmup_active else "READY",
                "count": summary.get("approved_count", 0),
            },
            {
                "name": "Paper fills",
                "status": "WAITING" if warmup_active else "READY",
                "count": summary.get("entry_fill_count", 0),
            },
            {
                "name": "Ledgers",
                "status": "WAITING" if warmup_active else "READY",
                "count": summary.get("completed_trade_count", 0),
            },
        ],
        "symbols": symbols,
        "trades": trades,
        "events": [_safe_event(event) for event in events[-60:]][::-1],
        "sessions": _session_history(),
    }
    with _cache_lock:
        _snapshot_cache[cache_key] = (time.monotonic(), snapshot)
    return snapshot


class ControlRoomHandler(BaseHTTPRequestHandler):
    server_version = "Stage1ControlRoom/1.0"

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")

    def _json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._json(
                HTTPStatus.OK,
                {"status": "ok", "paperOnly": True, "project": str(PROJECT_ROOT)},
            )
            return
        if parsed.path != "/api/snapshot":
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        try:
            requested_date = parse_qs(parsed.query).get("date", [None])[0]
            self._json(HTTPStatus.OK, build_snapshot(requested_date))
        except FileNotFoundError as error:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(error)})
        except ValueError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception:
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "The local snapshot could not be assembled."},
            )

    def do_POST(self) -> None:  # noqa: N802
        self._json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": "This control room is strictly read-only."},
        )

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the read-only Stage 1 control room API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    arguments = parser.parse_args()
    server = ThreadingHTTPServer((arguments.host, arguments.port), ControlRoomHandler)
    print(
        f"Stage 1 control room API is read-only at "
        f"http://{arguments.host}:{arguments.port}/api/health",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
