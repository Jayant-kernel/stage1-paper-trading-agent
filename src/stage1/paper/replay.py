from __future__ import annotations

import bisect
import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from stage1.config import PaperConfig
from stage1.paper.costs import CostTable, calculate_intraday_charges
from stage1.schemas import (
    DeterministicRiskDecision,
    PaperFill,
    PaperOrderIntent,
    PaperRewardRecord,
    PaperTradeOutcome,
)

_IST = ZoneInfo("Asia/Kolkata")
_NO_LLM_HASH = hashlib.sha256(b"DETERMINISTIC_BASELINE_NO_LLM").hexdigest()


@dataclass(frozen=True)
class ReplayResult:
    risk_decisions: pl.DataFrame
    intents: pl.DataFrame
    fills: pl.DataFrame
    outcomes: pl.DataFrame
    rewards: pl.DataFrame
    rejections: pl.DataFrame
    candidate_count: int
    approved_count: int
    hold_count: int
    entry_fill_count: int
    completed_trade_count: int
    unresolved_position_count: int
    ending_equity_by_profile: dict[str, float]

    def summary(self) -> dict[str, Any]:
        return {
            "candidate_count": self.candidate_count,
            "approved_count": self.approved_count,
            "hold_count": self.hold_count,
            "entry_fill_count": self.entry_fill_count,
            "completed_trade_count": self.completed_trade_count,
            "unresolved_position_count": self.unresolved_position_count,
            "ending_equity_by_profile": self.ending_equity_by_profile,
        }


@dataclass(frozen=True)
class ReplayArtifacts:
    report_relative_path: str
    report_sha256: str
    parquet_artifacts: dict[str, str]


@dataclass(frozen=True)
class PreparedReplayData:
    """Session market data indexed once for repeated offline variant replays."""

    feature_by_hash: dict[str, dict[str, Any]]
    ticks_by_symbol: dict[str, list[dict[str, Any]]]
    volumes_by_symbol: dict[str, list[tuple[datetime, float]]]


@dataclass
class _Position:
    candidate_id: str
    symbol: str
    direction: str
    sector: str
    stop_trigger: float
    entry: PaperFill
    initial_risk: float
    next_tick_index: int


def run_paper_replay(
    *,
    candidates: pl.DataFrame,
    features: pl.DataFrame,
    ticks: pl.DataFrame,
    bars: pl.DataFrame,
    sectors: dict[str, str],
    paper: PaperConfig,
    cost_table: CostTable,
    flatten_at: time,
    stale_tick_seconds: int,
    starting_equity_by_profile: dict[str, float] | None = None,
    prepared_data: PreparedReplayData | None = None,
) -> ReplayResult:
    if candidates.is_empty():
        raise ValueError("paper replay needs at least one deterministic candidate")
    prepared = prepared_data or prepare_replay_data(
        features=features,
        ticks=ticks,
        bars=bars,
    )
    feature_by_hash = prepared.feature_by_hash
    ticks_by_symbol = prepared.ticks_by_symbol
    volumes_by_symbol = prepared.volumes_by_symbol
    primary_profile = paper.cost_profiles[0]
    starting_equity = {
        name: float((starting_equity_by_profile or {}).get(name, paper.starting_cash))
        for name in paper.cost_profiles
    }
    if any(value <= 0 for value in starting_equity.values()):
        raise ValueError("paper starting equity must be positive")

    risk_rows: list[dict[str, Any]] = []
    intent_rows: list[dict[str, Any]] = []
    fill_rows: list[dict[str, Any]] = []
    outcome_rows: list[dict[str, Any]] = []
    reward_rows: list[dict[str, Any]] = []
    rejection_rows: list[dict[str, Any]] = []
    positions: dict[str, _Position] = {}
    entries_started = 0
    loss_streak = 0
    realized_by_profile = {name: 0.0 for name in paper.cost_profiles}

    def close_position(position: _Position, exit_fill: PaperFill, reason: str) -> None:
        nonlocal loss_streak
        fill_rows.append(exit_fill.model_dump(mode="json"))
        entry_turnover = position.entry.fill_price * position.entry.quantity
        exit_turnover = exit_fill.fill_price * exit_fill.quantity
        if position.direction == "LONG":
            buy_turnover, sell_turnover = entry_turnover, exit_turnover
            gross = (exit_fill.fill_price - position.entry.fill_price) * exit_fill.quantity
        else:
            buy_turnover, sell_turnover = exit_turnover, entry_turnover
            gross = (position.entry.fill_price - exit_fill.fill_price) * exit_fill.quantity
        for profile_name in paper.cost_profiles:
            charges = calculate_intraday_charges(
                table=cost_table,
                profile_name=profile_name,
                buy_turnover=buy_turnover,
                sell_turnover=sell_turnover,
            )
            net = gross - charges.total
            outcome_material = {
                "candidate_id": position.candidate_id,
                "profile": profile_name,
                "entry_fill": position.entry.fill_id,
                "exit_fill": exit_fill.fill_id,
                "cost_version": charges.cost_version,
            }
            outcome_id = _hash(outcome_material)
            outcome = PaperTradeOutcome(
                outcome_id=outcome_id,
                candidate_id=position.candidate_id,
                symbol=position.symbol,
                direction=position.direction,
                cost_profile=profile_name,
                entry_at=position.entry.filled_at,
                exit_at=exit_fill.filled_at,
                exit_reason=reason,
                quantity=exit_fill.quantity,
                entry_price=position.entry.fill_price,
                exit_price=exit_fill.fill_price,
                initial_risk=position.initial_risk,
                gross_pnl=gross,
                explicit_costs=charges.total,
                spread_cost=position.entry.spread_cost + exit_fill.spread_cost,
                slippage_cost=position.entry.slippage_cost + exit_fill.slippage_cost,
                net_pnl=net,
            )
            outcome_row = outcome.model_dump(mode="json") | charges.to_dict()
            outcome_rows.append(outcome_row)
            realized_by_profile[profile_name] += net
            reward_r = net / position.initial_risk
            if net > 0.005:
                label = "CORRECT_AFTER_COSTS"
                tags: list[str] = []
            elif net < -0.005:
                label = "INCORRECT_AFTER_COSTS"
                tags = ["STOP_EXIT" if reason == "STOP" else "NEGATIVE_AFTER_COSTS"]
                if gross > 0:
                    tags.append("COSTS_ERASED_EDGE")
            else:
                label = "BREAKEVEN"
                tags = []
            reward = PaperRewardRecord(
                reward_id=_hash({"outcome_id": outcome_id, "reward_version": "review_reward_v1"}),
                outcome_id=outcome_id,
                candidate_id=position.candidate_id,
                cost_profile=profile_name,
                outcome_known_at=exit_fill.filled_at,
                label=label,
                reward_r_multiple=reward_r,
                bonus_points=min(max(reward_r, 0.0), 2.0),
                mistake_tags=tags,
            )
            reward_rows.append(reward.model_dump(mode="json"))
            if profile_name == primary_profile:
                loss_streak = loss_streak + 1 if net < 0 else 0

    def settle_stops(until: datetime) -> None:
        for symbol, position in list(positions.items()):
            symbol_ticks = ticks_by_symbol.get(symbol, [])
            stop_index = _find_stop_tick(position, symbol_ticks, until)
            if stop_index is None:
                position.next_tick_index = bisect.bisect_right(
                    [row["received_ts"] for row in symbol_ticks], until
                )
                continue
            tick = symbol_ticks[stop_index]
            action = "SELL" if position.direction == "LONG" else "BUY"
            exit_fill = _make_fill(
                candidate_id=position.candidate_id,
                tick=tick,
                action=action,
                purpose="STOP",
                quantity=position.entry.quantity,
                requested_quantity=position.entry.quantity,
                paper=paper,
            )
            close_position(position, exit_fill, "STOP")
            del positions[symbol]

    for candidate in candidates.sort(["created_at", "score", "symbol"], descending=[False, True, False]).to_dicts():
        created_at = _timestamp(candidate["created_at"])
        settle_stops(created_at)
        feature = feature_by_hash[str(candidate["snapshot_hash"])]
        symbol = str(candidate["symbol"])
        estimate = float(feature["close"])
        atr = float(feature["atr_14"])
        sector = sectors.get(symbol, "UNKNOWN")
        hold_reasons: list[str] = []
        if entries_started >= paper.max_round_trips_per_day:
            hold_reasons.append("round_trip_limit")
        if len(positions) >= paper.max_open_positions:
            hold_reasons.append("open_position_limit")
        if symbol in positions:
            hold_reasons.append("duplicate_symbol_position")
        if any(position.sector == sector for position in positions.values()):
            hold_reasons.append("sector_position_limit")
        if loss_streak >= 3:
            hold_reasons.append("three_consecutive_losses")
        if realized_by_profile[primary_profile] <= -(
            starting_equity[primary_profile] * paper.max_daily_loss_fraction
        ):
            hold_reasons.append("daily_loss_stop")
        latest = _latest_tick_at_or_before(ticks_by_symbol.get(symbol, []), created_at)
        if latest is None or (created_at - latest["received_ts"]).total_seconds() > stale_tick_seconds:
            hold_reasons.append("stale_or_missing_quote")

        stop_distance = max(paper.stop_atr_multiple * atr, 2 * paper.tick_size)
        long = candidate["side"] == "LONG_CANDIDATE"
        stop_trigger = estimate - stop_distance if long else estimate + stop_distance
        equity = starting_equity[primary_profile] + realized_by_profile[primary_profile]
        risk_budget = equity * paper.risk_per_trade_fraction
        qty_risk = int(risk_budget // stop_distance)
        qty_capital = int((equity * paper.max_position_fraction) // estimate)
        quantity = min(qty_risk, qty_capital)
        if quantity <= 0 or stop_trigger <= 0:
            hold_reasons.append("invalid_or_zero_position_size")

        status = "HOLD" if hold_reasons else "APPROVED"
        decision_material = {
            "risk_version": "baseline_risk_v1",
            "candidate_id": candidate["candidate_id"],
            "status": status,
            "quantity": 0 if hold_reasons else quantity,
            "stop_trigger": None if hold_reasons else stop_trigger,
        }
        decision = DeterministicRiskDecision(
            decision_id=_hash(decision_material),
            candidate_id=candidate["candidate_id"],
            symbol=symbol,
            candidate_side=candidate["side"],
            status=status,
            decided_at=created_at,
            entry_estimate=estimate,
            stop_trigger=None if hold_reasons else stop_trigger,
            quantity=0 if hold_reasons else quantity,
            reasons=hold_reasons or ["all_baseline_risk_checks_passed"],
        )
        risk_rows.append(decision.model_dump(mode="json"))
        if hold_reasons:
            continue

        action = "BUY" if long else "SELL"
        intent_id = _hash(
            {
                "candidate_id": candidate["candidate_id"],
                "risk_decision_id": decision.decision_id,
                "portfolio": "DETERMINISTIC_BASELINE",
            }
        )
        intent = PaperOrderIntent(
            intent_id=intent_id,
            portfolio_id="DETERMINISTIC_BASELINE",
            symbol=symbol,
            side=action,
            quantity=quantity,
            entry_limit=estimate,
            stop_trigger=stop_trigger,
            created_at=created_at,
            valid_until=min(
                _timestamp(candidate["valid_until"]),
                created_at + timedelta(seconds=paper.fill_deadline_seconds),
            ),
            candidate_packet_hash=candidate["snapshot_hash"],
            model_response_hash=_NO_LLM_HASH,
        )
        intent_rows.append(intent.model_dump(mode="json"))
        eligible = _first_eligible_tick(
            ticks_by_symbol.get(symbol, []),
            after=created_at,
            deadline=intent.valid_until,
        )
        if eligible is None:
            rejection_rows.append(
                {"intent_id": intent_id, "candidate_id": candidate["candidate_id"], "reason": "NO_QUOTE_WITHIN_DEADLINE"}
            )
            continue
        quote = _execution_quote(eligible, action, paper)
        if abs(quote - estimate) > paper.max_entry_move_atr * atr:
            rejection_rows.append(
                {"intent_id": intent_id, "candidate_id": candidate["candidate_id"], "reason": "ENTRY_MOVED_MORE_THAN_0_25_ATR"}
            )
            continue
        volume_cap = _recent_volume_cap(
            volumes_by_symbol.get(symbol, []),
            eligible["received_ts"],
            paper.unknown_depth_volume_fraction,
        )
        displayed = eligible.get("ask_qty" if action == "BUY" else "bid_qty")
        liquidity_cap = max(1, int(displayed)) if displayed is not None else volume_cap
        fill_quantity = min(quantity, liquidity_cap)
        entry_fill = _make_fill(
            candidate_id=candidate["candidate_id"],
            tick=eligible,
            action=action,
            purpose="ENTRY",
            quantity=fill_quantity,
            requested_quantity=quantity,
            paper=paper,
        )
        fill_rows.append(entry_fill.model_dump(mode="json"))
        entries_started += 1
        tick_index = ticks_by_symbol[symbol].index(eligible)
        positions[symbol] = _Position(
            candidate_id=candidate["candidate_id"],
            symbol=symbol,
            direction="LONG" if long else "SHORT",
            sector=sector,
            stop_trigger=stop_trigger,
            entry=entry_fill,
            initial_risk=max(abs(entry_fill.fill_price - stop_trigger) * fill_quantity, 0.0001),
            next_tick_index=tick_index + 1,
        )

    session_date = _timestamp(candidates["created_at"].min()).astimezone(_IST).date()
    flatten_timestamp = datetime.combine(session_date, flatten_at, tzinfo=_IST)
    settle_stops(flatten_timestamp)
    for symbol, position in list(positions.items()):
        eligible = _first_eligible_tick(
            ticks_by_symbol.get(symbol, []),
            after=flatten_timestamp,
            deadline=datetime.combine(session_date, time(15, 30), tzinfo=_IST),
        )
        if eligible is None:
            continue
        action = "SELL" if position.direction == "LONG" else "BUY"
        exit_fill = _make_fill(
            candidate_id=position.candidate_id,
            tick=eligible,
            action=action,
            purpose="FLATTEN",
            quantity=position.entry.quantity,
            requested_quantity=position.entry.quantity,
            paper=paper,
        )
        close_position(position, exit_fill, "FLATTEN")
        del positions[symbol]

    risk_frame = pl.DataFrame(risk_rows)
    outcomes_frame = pl.DataFrame(outcome_rows)
    ending = {
        profile: round(starting_equity[profile] + value, 4)
        for profile, value in realized_by_profile.items()
    }
    return ReplayResult(
        risk_decisions=risk_frame,
        intents=pl.DataFrame(intent_rows),
        fills=pl.DataFrame(fill_rows),
        outcomes=outcomes_frame,
        rewards=pl.DataFrame(reward_rows),
        rejections=pl.DataFrame(rejection_rows),
        candidate_count=candidates.height,
        approved_count=risk_frame.filter(pl.col("status") == "APPROVED").height,
        hold_count=risk_frame.filter(pl.col("status") == "HOLD").height,
        entry_fill_count=sum(row["purpose"] == "ENTRY" for row in fill_rows),
        completed_trade_count=(outcomes_frame.height // len(paper.cost_profiles)) if not outcomes_frame.is_empty() else 0,
        unresolved_position_count=len(positions),
        ending_equity_by_profile=ending,
    )


def prepare_replay_data(
    *, features: pl.DataFrame, ticks: pl.DataFrame, bars: pl.DataFrame
) -> PreparedReplayData:
    return PreparedReplayData(
        feature_by_hash={
            str(row["snapshot_hash"]): row for row in features.to_dicts()
        },
        ticks_by_symbol=_prepare_ticks(ticks),
        volumes_by_symbol=_prepare_volumes(bars),
    )


def write_replay_artifacts(
    *, project_root: str | Path, session_date: str, result: ReplayResult
) -> ReplayArtifacts:
    root = Path(project_root).resolve()
    partition = root / "data" / "decisions" / f"date={session_date}"
    partition.mkdir(parents=True, exist_ok=True)
    parquet_artifacts: dict[str, str] = {}
    for name, frame in {
        "risk-decisions": result.risk_decisions,
        "paper-intents": result.intents,
        "paper-fills": result.fills,
        "paper-outcomes": result.outcomes,
        "paper-rewards": result.rewards,
        "paper-rejections": result.rejections,
    }.items():
        if frame.is_empty():
            continue
        temporary = partition / f".tmp-{uuid.uuid4().hex}.parquet"
        frame.write_parquet(temporary, compression="zstd", statistics=True)
        digest = _sha256_file(temporary)
        destination = partition / f"{name}-{digest[:16]}.parquet"
        if destination.exists():
            temporary.unlink()
        else:
            os.replace(temporary, destination)
        parquet_artifacts[name] = destination.relative_to(root).as_posix()
    report_bytes = (
        json.dumps(result.summary(), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    report_hash = hashlib.sha256(report_bytes).hexdigest()
    report_path = partition / f"paper-replay-report-{report_hash[:16]}.json"
    if not report_path.exists():
        temporary_report = report_path.with_suffix(".tmp")
        temporary_report.write_bytes(report_bytes)
        os.replace(temporary_report, report_path)
    return ReplayArtifacts(
        report_relative_path=report_path.relative_to(root).as_posix(),
        report_sha256=report_hash,
        parquet_artifacts=parquet_artifacts,
    )


def _prepare_ticks(frame: pl.DataFrame) -> dict[str, list[dict[str, Any]]]:
    required = {"symbol", "received_ts", "ltp", "bid", "ask", "bid_qty", "ask_qty", "provider_message_hash"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"tick frame is missing columns: {missing}")
    unique: dict[str, dict[str, Any]] = {}
    for row in frame.to_dicts():
        row["received_ts"] = _timestamp(row["received_ts"])
        message_hash = str(row["provider_message_hash"])
        prior = unique.get(message_hash)
        if prior is None or row["received_ts"] < prior["received_ts"]:
            unique[message_hash] = row
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in unique.values():
        grouped.setdefault(str(row["symbol"]), []).append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: (row["received_ts"], row["provider_message_hash"]))
    return grouped


def _prepare_volumes(frame: pl.DataFrame) -> dict[str, list[tuple[datetime, float]]]:
    grouped: dict[str, list[tuple[datetime, float]]] = {}
    for row in frame.select(["symbol", "bar_end", "volume"]).to_dicts():
        if row["volume"] is not None:
            grouped.setdefault(str(row["symbol"]), []).append(
                (_timestamp(row["bar_end"]), float(row["volume"]))
            )
    for rows in grouped.values():
        rows.sort()
    return grouped


def _latest_tick_at_or_before(rows: list[dict[str, Any]], at: datetime) -> dict[str, Any] | None:
    times = [row["received_ts"] for row in rows]
    index = bisect.bisect_right(times, at) - 1
    return rows[index] if index >= 0 else None


def _first_eligible_tick(
    rows: list[dict[str, Any]], *, after: datetime, deadline: datetime
) -> dict[str, Any] | None:
    times = [row["received_ts"] for row in rows]
    index = bisect.bisect_right(times, after)
    while index < len(rows) and rows[index]["received_ts"] <= deadline:
        if float(rows[index]["ltp"]) > 0:
            return rows[index]
        index += 1
    return None


def _find_stop_tick(
    position: _Position, rows: list[dict[str, Any]], until: datetime
) -> int | None:
    for index in range(position.next_tick_index, len(rows)):
        row = rows[index]
        if row["received_ts"] > until:
            return None
        price = float(row["ltp"])
        crossed = price <= position.stop_trigger if position.direction == "LONG" else price >= position.stop_trigger
        if crossed:
            return index
    return None


def _execution_quote(tick: dict[str, Any], action: str, paper: PaperConfig) -> float:
    quote = tick.get("ask" if action == "BUY" else "bid")
    if quote is not None and float(quote) > 0:
        return float(quote)
    ltp = float(tick["ltp"])
    half_spread = max(paper.tick_size, ltp * paper.fallback_half_spread_bps / 10_000)
    return ltp + half_spread if action == "BUY" else max(ltp - half_spread, paper.tick_size)


def _make_fill(
    *, candidate_id: str, tick: dict[str, Any], action: str, purpose: str,
    quantity: int, requested_quantity: int, paper: PaperConfig
) -> PaperFill:
    observed = _execution_quote(tick, action, paper)
    multiplier = 1 + paper.default_slippage_bps / 10_000 if action == "BUY" else 1 - paper.default_slippage_bps / 10_000
    fill_price = observed * multiplier
    bid, ask = tick.get("bid"), tick.get("ask")
    if bid is not None and ask is not None and float(bid) > 0 and float(ask) > 0:
        midpoint = (float(bid) + float(ask)) / 2
        spread_cost = abs(observed - midpoint) * quantity
    else:
        spread_cost = abs(observed - float(tick["ltp"])) * quantity
    slippage_cost = abs(fill_price - observed) * quantity
    material = {
        "candidate_id": candidate_id,
        "message_hash": tick["provider_message_hash"],
        "action": action,
        "purpose": purpose,
        "quantity": quantity,
        "fill_price": round(fill_price, 10),
    }
    return PaperFill(
        fill_id=_hash(material),
        candidate_id=candidate_id,
        symbol=str(tick["symbol"]),
        action=action,
        purpose=purpose,
        filled_at=tick["received_ts"],
        quantity=quantity,
        observed_quote=observed,
        fill_price=fill_price,
        spread_cost=spread_cost,
        slippage_cost=slippage_cost,
        provider_message_hash=str(tick["provider_message_hash"]),
        partial=quantity < requested_quantity,
    )


def _recent_volume_cap(rows: list[tuple[datetime, float]], at: datetime, fraction: float) -> int:
    times = [row[0] for row in rows]
    index = bisect.bisect_right(times, at) - 1
    if index < 0:
        return 1
    return max(1, int(rows[index][1] * fraction))


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
