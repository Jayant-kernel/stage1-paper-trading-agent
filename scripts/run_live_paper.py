from __future__ import annotations

import argparse
import csv
import hashlib
import json
import msvcrt
import os
import stat
import sys
import time as time_module
from dataclasses import asdict, replace
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stage1.alerts.telegram import TelegramOutboundClient  # noqa: E402
from stage1.config import Stage1Config, load_config  # noqa: E402
from stage1.market.bar_builder import build_minute_bars, write_bar_artifact  # noqa: E402
from stage1.market.features import build_market_features, write_feature_artifact  # noqa: E402
from stage1.paper.costs import load_cost_table  # noqa: E402
from stage1.paper.replay import (  # noqa: E402
    ReplayResult,
    run_paper_replay,
    write_replay_artifacts,
)
from stage1.schemas import RunCard  # noqa: E402
from stage1.secrets import load_secrets  # noqa: E402
from stage1.strategy.candidate_gate import (  # noqa: E402
    generate_baseline_candidates,
    write_candidate_artifact,
)

IST = ZoneInfo("Asia/Kolkata")


class SessionLock:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("a+b")
        if path.stat().st_size == 0:
            self._handle.write(b"0")
            self._handle.flush()
        self._handle.seek(0)
        try:
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self._handle.close()
            raise RuntimeError("today's paper engine is already running") from exc

    def close(self) -> None:
        if self._handle.closed:
            return
        self._handle.seek(0)
        msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        self._handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic paper-only engine over live FYERS data files.")
    parser.add_argument("--session-date", default=datetime.now(IST).date().isoformat())
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--finalize-only",
        action="store_true",
        help="Recover immutable end-of-day artifacts without requiring a fresh feed.",
    )
    args = parser.parse_args()
    if args.poll_seconds < 5:
        raise SystemExit("poll interval must be at least five seconds")

    config = load_config(ROOT / "config" / "stage1.yaml")
    if config.mode != "paper" or config.live_order_endpoints_enabled:
        raise SystemExit("paper-only configuration check failed")
    halt_path = ROOT / config.operations.halt_file
    if halt_path.exists():
        raise SystemExit("HALT_STAGE1 is present; paper engine did not start")
    tick_root = ROOT / "data" / "raw" / "ticks" / f"date={args.session_date}"
    if not tick_root.is_dir():
        raise SystemExit("today's FYERS raw-data directory is missing")

    try:
        lock = SessionLock(ROOT / "state" / f"live-paper-{args.session_date}.lock")
    except RuntimeError as exc:
        print(json.dumps({"status": "not_started", "reason": str(exc)}))
        return 3
    state_path = ROOT / "state" / f"live-paper-{args.session_date}.json"
    journal_path = ROOT / "data" / "decisions" / f"date={args.session_date}" / "live-paper-events.jsonl"
    prior_state = _read_json(state_path)
    started_at = _timestamp(prior_state.get("started_at")) if prior_state else datetime.now(timezone.utc)
    starting_balances = (
        prior_state.get("starting_equity_by_profile")
        if prior_state
        else _previous_balances(args.session_date, config)
    )
    known_events = _known_events(journal_path)
    if args.finalize_only:
        try:
            _finalize(
                session_date=args.session_date,
                started_at=started_at,
                config=config,
                starting_balances=starting_balances,
                state_path=state_path,
            )
        finally:
            lock.close()
        return 0
    _write_state(
        state_path,
        {
            "status": "STARTING",
            "session_date": args.session_date,
            "started_at": started_at.isoformat(),
            "starting_equity_by_profile": starting_balances,
            "paper_only": True,
            "live_order_endpoints_enabled": False,
            "baseline_version": "baseline_v1/baseline_risk_v1/next_observable_v1",
        },
    )
    _send_alert(
        f"Stage 1 deterministic paper engine started for {args.session_date}. "
        "Paper-only; Qwen, news, memory and cloud decisions are disabled."
    )

    exit_code = 0
    try:
        while True:
            now = datetime.now(timezone.utc)
            if halt_path.exists():
                _write_state(state_path, _read_json(state_path) | {"status": "HALTED", "heartbeat_at": now.isoformat()})
                exit_code = 2
                break
            try:
                cycle = _run_cycle(
                    session_date=args.session_date,
                    started_at=started_at,
                    now=now,
                    config=config,
                    starting_balances=starting_balances,
                )
                _append_new_events(journal_path, cycle, known_events, now)
                _write_state(
                    state_path,
                    {
                        "status": cycle["status"],
                        "session_date": args.session_date,
                        "started_at": started_at.isoformat(),
                        "heartbeat_at": now.isoformat(),
                        "starting_equity_by_profile": starting_balances,
                        "paper_only": True,
                        "live_order_endpoints_enabled": False,
                        "baseline_version": "baseline_v1/baseline_risk_v1/next_observable_v1",
                        "active_symbols": config.universe.all_symbols,
                        "summary": cycle.get("summary", {}),
                        "raw_tick_files": cycle["raw_tick_files"],
                        "latest_raw_file_at": cycle["latest_raw_file_at"],
                    },
                )
                print(
                    json.dumps(
                        {
                            "event": "paper_engine_heartbeat",
                            "status": cycle["status"],
                            "session_date": args.session_date,
                            "summary": cycle.get("summary", {}),
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            except Exception as exc:
                _write_state(
                    state_path,
                    _read_json(state_path)
                    | {
                        "status": "SAFE_HOLD",
                        "heartbeat_at": now.isoformat(),
                        "error": type(exc).__name__,
                    },
                )
                print(
                    json.dumps(
                        {
                            "event": "paper_engine_safe_hold",
                            "status": "SAFE_HOLD",
                            "reason": type(exc).__name__,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            if args.once:
                break
            shutdown = datetime.combine(
                datetime.fromisoformat(args.session_date).date(),
                config.market.shutdown,
                tzinfo=IST,
            )
            if datetime.now(IST) >= shutdown:
                break
            time_module.sleep(args.poll_seconds)

        if not args.once and exit_code == 0:
            _finalize(
                session_date=args.session_date,
                started_at=started_at,
                config=config,
                starting_balances=starting_balances,
                state_path=state_path,
            )
    except KeyboardInterrupt:
        _write_state(state_path, _read_json(state_path) | {"status": "STOPPED_EARLY", "heartbeat_at": datetime.now(timezone.utc).isoformat()})
    finally:
        lock.close()
    return exit_code


def _run_cycle(
    *, session_date: str, started_at: datetime, now: datetime,
    config: Stage1Config, starting_balances: dict[str, float]
) -> dict[str, Any]:
    tick_files = sorted((ROOT / "data" / "raw" / "ticks" / f"date={session_date}").rglob("*.parquet"))
    if not tick_files:
        raise RuntimeError("no raw tick files")
    newest = max(path.stat().st_mtime for path in tick_files)
    newest_at = datetime.fromtimestamp(newest, tz=timezone.utc)
    if (now - newest_at).total_seconds() > 120:
        raise RuntimeError("FYERS raw data is stale")
    ticks = pl.read_parquet(tick_files)
    bars_result = build_minute_bars(ticks, stale_tick_seconds=config.data.stale_tick_seconds)
    complete_bars = bars_result.frame.filter(
        pl.col("bar_end").str.to_datetime(format="%+", time_zone="UTC") <= now
    )
    if complete_bars.height < 60:
        return {
            "status": "WARMING_UP",
            "raw_tick_files": len(tick_files),
            "latest_raw_file_at": newest_at.isoformat(),
            "frames": {"candidates": pl.DataFrame()},
            "summary": {"complete_bars": complete_bars.height},
        }
    features = build_market_features(complete_bars)
    if features.frame.is_empty():
        return {
            "status": "WARMING_UP",
            "raw_tick_files": len(tick_files),
            "latest_raw_file_at": newest_at.isoformat(),
            "frames": {"candidates": pl.DataFrame()},
            "summary": {
                "complete_bars": complete_bars.height,
                "feature_snapshots": 0,
            },
        }
    candidates = _generate_candidates(features.frame, config)
    if not candidates.is_empty():
        candidates = candidates.filter(
            pl.col("created_at").str.to_datetime(format="%+", time_zone="UTC") >= started_at
        ).filter(
            pl.col("created_at").str.to_datetime(format="%+", time_zone="UTC") <= now
        )
    frames: dict[str, pl.DataFrame] = {"candidates": candidates}
    if candidates.is_empty():
        return {
            "status": "MONITORING",
            "raw_tick_files": len(tick_files),
            "latest_raw_file_at": newest_at.isoformat(),
            "frames": frames,
            "summary": {"complete_bars": complete_bars.height, "feature_snapshots": features.frame.height, "candidate_count": 0},
        }
    result = run_paper_replay(
        candidates=candidates,
        features=features.frame,
        ticks=ticks,
        bars=complete_bars,
        sectors=_sectors(),
        paper=config.paper,
        cost_table=load_cost_table(ROOT / "config" / "cost_profiles.yaml"),
        flatten_at=config.market.flatten,
        stale_tick_seconds=config.data.stale_tick_seconds,
        starting_equity_by_profile=starting_balances,
    )
    frames |= {
        "risk_decisions": result.risk_decisions,
        "intents": result.intents,
        "fills": result.fills,
        "outcomes": result.outcomes,
        "rewards": result.rewards,
        "rejections": result.rejections,
    }
    return {
        "status": "PAPER_ACTIVE",
        "raw_tick_files": len(tick_files),
        "latest_raw_file_at": newest_at.isoformat(),
        "frames": frames,
        "summary": result.summary() | {"complete_bars": complete_bars.height, "feature_snapshots": features.frame.height},
    }


def _generate_candidates(features: pl.DataFrame, config: Stage1Config) -> pl.DataFrame:
    return generate_baseline_candidates(
        features,
        trade_symbols=config.universe.symbols,
        candidate_start=config.market.candidate_start,
        entry_cutoff=config.market.new_entry_cutoff,
        cadence_minutes=config.candidate_gate.cadence_minutes,
        max_candidates_per_gate=config.candidate_gate.max_candidates,
        min_price=config.candidate_gate.min_price,
        max_spread_bps=config.candidate_gate.max_estimated_spread_bps,
        min_volume_z=config.candidate_gate.min_volume_z,
        cooldown_minutes=config.candidate_gate.cooldown_minutes_after_decision,
    ).frame


def _append_new_events(path: Path, cycle: dict[str, Any], known: set[str], observed_at: datetime) -> None:
    id_columns = {
        "candidates": "candidate_id", "risk_decisions": "decision_id",
        "intents": "intent_id", "fills": "fill_id", "outcomes": "outcome_id",
        "rewards": "reward_id",
    }
    pending: list[dict[str, Any]] = []
    for event_type, frame in cycle.get("frames", {}).items():
        if frame.is_empty():
            continue
        for payload in frame.to_dicts():
            event_id = str(payload.get(id_columns.get(event_type, "")) or _hash(payload))
            key = f"{event_type}:{event_id}"
            if key not in known:
                known.add(key)
                pending.append({"event_type": event_type, "event_id": event_id, "recorded_at": observed_at.isoformat(), "payload": payload})
    if not pending:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in pending:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _known_events(path: Path) -> set[str]:
    if not path.exists():
        return set()
    known: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
                known.add(f"{row['event_type']}:{row['event_id']}")
            except (json.JSONDecodeError, KeyError):
                raise RuntimeError("live paper journal is corrupt") from None
    return known


def _finalize(
    *, session_date: str, started_at: datetime, config: Stage1Config,
    starting_balances: dict[str, float], state_path: Path
) -> None:
    now = datetime.now(timezone.utc)
    tick_files = sorted((ROOT / "data" / "raw" / "ticks" / f"date={session_date}").rglob("*.parquet"))
    if not tick_files:
        raise RuntimeError("cannot finalize without raw tick files")
    ticks = pl.read_parquet(tick_files)
    latest_raw_tick_received_at = str(ticks["received_ts"].max())
    bars_result = build_minute_bars(ticks, stale_tick_seconds=config.data.stale_tick_seconds)
    close_at = datetime.combine(datetime.fromisoformat(session_date).date(), time(15, 30), tzinfo=IST).astimezone(timezone.utc)
    bars_result = replace(
        bars_result,
        frame=bars_result.frame.filter(pl.col("bar_end").str.to_datetime(format="%+", time_zone="UTC") <= close_at),
    )
    bar_artifact = write_bar_artifact(project_root=ROOT, session_date=session_date, result=bars_result)
    feature_result = build_market_features(bars_result.frame)
    feature_artifact = write_feature_artifact(project_root=ROOT, session_date=session_date, result=feature_result)
    candidate_result = generate_baseline_candidates(
        feature_result.frame,
        trade_symbols=config.universe.symbols,
        candidate_start=config.market.candidate_start,
        entry_cutoff=config.market.new_entry_cutoff,
        cadence_minutes=config.candidate_gate.cadence_minutes,
        max_candidates_per_gate=config.candidate_gate.max_candidates,
        min_price=config.candidate_gate.min_price,
        max_spread_bps=config.candidate_gate.max_estimated_spread_bps,
        min_volume_z=config.candidate_gate.min_volume_z,
        cooldown_minutes=config.candidate_gate.cooldown_minutes_after_decision,
    )
    candidates = candidate_result.frame
    if not candidates.is_empty():
        candidates = candidates.filter(
            pl.col("created_at").str.to_datetime(format="%+", time_zone="UTC")
            >= started_at
        )
    candidate_result = replace(candidate_result, frame=candidates)
    if candidates.is_empty():
        final_state = _read_json(state_path)
        final_state.pop("error", None)
        final_state.pop("latest_raw_file_at", None)
        _write_state(
            state_path,
            final_state
            | {
                "status": "FINALIZED_NO_TRADES",
                "finished_at": now.isoformat(),
                "summary": {
                    "candidate_count": 0,
                    "completed_trade_count": 0,
                    "bar_count": bars_result.frame.height,
                    "feature_snapshot_count": feature_result.frame.height,
                },
                "bar_artifact": asdict(bar_artifact),
                "feature_artifact": asdict(feature_artifact),
                "raw_tick_files": len(tick_files),
                "latest_raw_tick_received_at": latest_raw_tick_received_at,
            },
        )
        _send_alert(
            f"Stage 1 paper session {session_date} finalized with no eligible "
            "candidates. No live orders were possible."
        )
        return
    candidate_artifact = write_candidate_artifact(project_root=ROOT, session_date=session_date, result=candidate_result)
    replay_args = dict(
        candidates=candidates, features=feature_result.frame, ticks=ticks,
        bars=bars_result.frame, sectors=_sectors(), paper=config.paper,
        cost_table=load_cost_table(ROOT / "config" / "cost_profiles.yaml"),
        flatten_at=config.market.flatten, stale_tick_seconds=config.data.stale_tick_seconds,
        starting_equity_by_profile=starting_balances,
    )
    first = run_paper_replay(**replay_args)
    second = run_paper_replay(**replay_args)
    if _replay_fingerprint(first) != _replay_fingerprint(second):
        raise RuntimeError("saved-day replay was not deterministic")
    replay_artifacts = write_replay_artifacts(project_root=ROOT, session_date=session_date, result=first)
    run_card_path = _write_run_card(
        session_date=session_date, started_at=started_at, finished_at=now,
        config=config, tick_files=tick_files, bar_artifact=asdict(bar_artifact),
        feature_artifact=asdict(feature_artifact), candidate_artifact=asdict(candidate_artifact),
        replay_artifacts=asdict(replay_artifacts), summary=first.summary(),
    )
    final_state = _read_json(state_path)
    final_state.pop("error", None)
    final_state.pop("latest_raw_file_at", None)
    _write_state(state_path, final_state | {"status": "FINALIZED", "finished_at": now.isoformat(), "summary": first.summary(), "run_card": run_card_path, "raw_tick_files": len(tick_files), "latest_raw_tick_received_at": latest_raw_tick_received_at})
    _send_alert(f"Stage 1 paper session {session_date} finalized and replay-verified. No live orders were possible.")


def _write_run_card(**values: Any) -> str:
    config: Stage1Config = values["config"]
    tick_hashes = [_sha256(path) for path in values["tick_files"]]
    data_hashes = tick_hashes + [values["bar_artifact"]["file_sha256"], values["feature_artifact"]["file_sha256"], values["candidate_artifact"]["file_sha256"]]
    prompt_hashes = {path.name: _sha256(path) for path in sorted((ROOT / "prompts").glob("*")) if path.is_file()}
    material = {
        "run_id": _hash({"session_date": values["session_date"], "started_at": values["started_at"]}),
        "parent_run_id": None,
        "mode": "DEVELOPMENT",
        "started_at": values["started_at"],
        "finished_at": values["finished_at"],
        "code_commit": "UNBORN_OR_UNAVAILABLE",
        "dirty_worktree": True,
        "config_hash": _sha256(ROOT / "config" / "stage1.yaml"),
        "prompt_hashes": prompt_hashes,
        "dependency_lock_hash": _sha256(ROOT / "requirements.lock"),
        "runtime_manifest_hash": _runtime_hash(),
        "model_name": "DISABLED_DETERMINISTIC_BASELINE",
        "model_digest": hashlib.sha256(b"NO_MODEL_USED").hexdigest(),
        "data_partition_hashes": data_hashes,
        "evidence_manifest_hash": _hash(tick_hashes),
        "experiment_id": "BASELINE_V1",
        "ablation_id": "DETERMINISTIC_NO_NEWS_NO_LLM_NO_MEMORY",
        "metrics_hash": _hash(values["summary"]),
        "report_hash": values["replay_artifacts"]["report_sha256"],
        "previous_run_card_hash": None,
        "required_evidence": {
            "paper_only": True, "live_order_endpoints_enabled": False,
            "replay_identical": True, "news_enabled": False, "qwen_enabled": False,
            "memory_enabled": False, "cloud_enabled": False,
            "bar_artifact": values["bar_artifact"],
            "feature_artifact": values["feature_artifact"],
            "candidate_artifact": values["candidate_artifact"],
            "replay_artifacts": values["replay_artifacts"],
        },
    }
    material["run_card_hash"] = _hash(material)
    card = RunCard.model_validate(material)
    payload = (card.model_dump_json(indent=2) + "\n").encode()
    path = ROOT / "data" / "run_cards" / f"date={values['session_date']}" / f"run-card-{card.run_card_hash[:16]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, path)
        os.chmod(path, stat.S_IREAD)
    return path.relative_to(ROOT).as_posix()


def _previous_balances(session_date: str, config: Stage1Config) -> dict[str, float]:
    reports = sorted((ROOT / "data" / "decisions").glob("date=*/paper-replay-report-*.json"), reverse=True)
    for path in reports:
        date_part = path.parent.name.removeprefix("date=")
        if date_part < session_date:
            payload = _read_json(path)
            balances = payload.get("ending_equity_by_profile")
            if isinstance(balances, dict):
                return {name: float(balances.get(name, config.paper.starting_cash)) for name in config.paper.cost_profiles}
    return {name: config.paper.starting_cash for name in config.paper.cost_profiles}


def _sectors() -> dict[str, str]:
    with (ROOT / "config" / "universe.csv").open(newline="", encoding="utf-8") as handle:
        return {row["symbol"]: row["sector"] for row in csv.DictReader(handle)}


def _send_alert(message: str) -> None:
    try:
        credentials = load_secrets(ROOT / ".env").require_telegram_alert_credentials()
        with TelegramOutboundClient(credentials) as client:
            client.send_message(message)
    except Exception:
        return


def _runtime_hash() -> str:
    rows = []
    for directory in ("src", "scripts", "config", "prompts"):
        for path in sorted((ROOT / directory).rglob("*")):
            if path.is_file() and path.name != ".env":
                rows.append((path.relative_to(ROOT).as_posix(), _sha256(path)))
    return _hash(rows)


def _replay_fingerprint(result: ReplayResult) -> str:
    return _hash({
        "summary": result.summary(),
        "risk": result.risk_decisions.to_dicts(), "intents": result.intents.to_dicts(),
        "fills": result.fills.to_dicts(), "outcomes": result.outcomes.to_dicts(),
        "rewards": result.rewards.to_dicts(), "rejections": result.rejections.to_dicts(),
    })


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
