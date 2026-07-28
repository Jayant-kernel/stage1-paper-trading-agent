from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


class NormalizedTick(FrozenModel):
    provider: Literal["FYERS"] = "FYERS"
    symbol: str = Field(min_length=1)
    exchange_ts: datetime
    received_ts: datetime
    ltp: float = Field(gt=0, allow_inf_nan=False)
    bid: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    ask: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    bid_qty: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    ask_qty: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    last_traded_qty: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cumulative_volume: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    provider_message_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("exchange_ts", "received_ts")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def quote_is_not_crossed(self) -> "NormalizedTick":
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")
        return self


class RawTickRecord(FrozenModel):
    tick: NormalizedTick
    raw_message_json: str = Field(min_length=2)
    schema_version: Literal["normalized_tick_v1"] = "normalized_tick_v1"


class MinuteBar(FrozenModel):
    schema_version: Literal["minute_bar_v1"] = "minute_bar_v1"
    provider: Literal["FYERS"] = "FYERS"
    symbol: str = Field(min_length=1)
    bar_start: datetime
    bar_end: datetime
    first_received_ts: datetime
    last_received_ts: datetime
    open: float = Field(gt=0, allow_inf_nan=False)
    high: float = Field(gt=0, allow_inf_nan=False)
    low: float = Field(gt=0, allow_inf_nan=False)
    close: float = Field(gt=0, allow_inf_nan=False)
    bid_close: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    ask_close: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    volume: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cumulative_volume_end: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    tick_count: int = Field(gt=0)
    duplicate_receipts_dropped: int = Field(ge=0)
    out_of_order_tick_count: int = Field(ge=0)
    stale_tick_count: int = Field(ge=0)
    clock_skew_tick_count: int = Field(ge=0)
    missing_minutes_before: int = Field(ge=0)
    quality_flags: list[str]
    bar_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("bar_start", "bar_end", "first_received_ts", "last_received_ts")
    @classmethod
    def bar_times_are_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def ohlc_and_times_are_consistent(self) -> "MinuteBar":
        if self.bar_end <= self.bar_start:
            raise ValueError("bar_end must be after bar_start")
        if self.last_received_ts < self.first_received_ts:
            raise ValueError("last_received_ts cannot precede first_received_ts")
        if self.low > min(self.open, self.close):
            raise ValueError("low cannot exceed open or close")
        if self.high < max(self.open, self.close):
            raise ValueError("high cannot be below open or close")
        if self.low > self.high:
            raise ValueError("low cannot exceed high")
        if (
            self.bid_close is not None
            and self.ask_close is not None
            and self.bid_close > self.ask_close
        ):
            raise ValueError("bar closing bid cannot exceed closing ask")
        return self


class QuarantinedMarketEvent(FrozenModel):
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: Literal["FYERS"] = "FYERS"
    received_ts: datetime
    reason_code: Literal["UNKNOWN_SYMBOL", "INVALID_MESSAGE"]
    reason: str = Field(min_length=1, max_length=300)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_message_json: str = Field(min_length=2)

    @field_validator("received_ts")
    @classmethod
    def quarantine_time_is_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)


class RawTickManifest(FrozenModel):
    manifest_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    relative_path: str
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: Literal["FYERS"] = "FYERS"
    symbol: str
    session_date: str
    row_count: int = Field(gt=0)
    first_exchange_ts: datetime
    last_exchange_ts: datetime
    first_received_ts: datetime
    last_received_ts: datetime
    created_at: datetime

    @field_validator(
        "first_exchange_ts",
        "last_exchange_ts",
        "first_received_ts",
        "last_received_ts",
        "created_at",
    )
    @classmethod
    def manifest_times_are_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)


class QuarantineManifest(FrozenModel):
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    relative_path: str
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: Literal["FYERS"] = "FYERS"
    received_ts: datetime
    reason_code: Literal["UNKNOWN_SYMBOL", "INVALID_MESSAGE"]
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @field_validator("received_ts", "created_at")
    @classmethod
    def quarantine_manifest_times_are_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)


class MarketSnapshot(FrozenModel):
    symbol: str
    observed_at: datetime
    available_at: datetime
    close: float
    bid: float | None
    ask: float | None
    vwap: float
    ema_9: float
    ema_21: float
    rsi_14: float
    atr_14: float
    volume_z: float
    return_5m_bps: float
    market_return_5m_bps: float
    data_quality_flags: list[str]

    @field_validator("observed_at", "available_at")
    @classmethod
    def observed_at_is_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def snapshot_cannot_be_available_before_observation(self) -> "MarketSnapshot":
        if self.available_at < self.observed_at:
            raise ValueError("snapshot cannot be available before its market observation")
        return self


class EvidenceItem(FrozenModel):
    evidence_id: str
    source: Literal["NSE", "SEBI", "RBI", "GDELT", "MARKET"]
    published_at: datetime
    first_seen_at: datetime
    symbol: str | None
    event_type: str
    sanitized_text: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("published_at", "first_seen_at")
    @classmethod
    def evidence_times_are_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)


class CandidatePacket(FrozenModel):
    packet_id: str
    prompt_version: str
    created_at: datetime
    evidence_cutoff_at: datetime
    valid_until: datetime
    snapshot: MarketSnapshot
    deterministic_signal: Literal["LONG_CANDIDATE", "SHORT_CANDIDATE"]
    deterministic_reasons: list[str] = Field(min_length=1)
    recent_events: list[EvidenceItem]

    @field_validator("created_at", "evidence_cutoff_at", "valid_until")
    @classmethod
    def packet_times_are_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def packet_time_order(self) -> "CandidatePacket":
        if self.evidence_cutoff_at > self.created_at:
            raise ValueError("evidence cutoff cannot be after packet creation")
        if self.valid_until <= self.created_at:
            raise ValueError("packet must be valid after creation")
        return self


class DeterministicCandidate(FrozenModel):
    candidate_version: str = Field(
        default="baseline_v1", pattern=r"^[a-z0-9][a-z0-9_]{2,63}$"
    )
    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbol: str = Field(min_length=1)
    side: Literal["LONG_CANDIDATE", "SHORT_CANDIDATE"]
    created_at: datetime
    valid_until: datetime
    score: float = Field(ge=0, le=1, allow_inf_nan=False)
    spread_bps: float = Field(ge=0, allow_inf_nan=False)
    relative_return_5m_bps: float = Field(allow_inf_nan=False)
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    deterministic_reasons: list[str] = Field(min_length=1)

    @field_validator("created_at", "valid_until")
    @classmethod
    def candidate_times_are_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def candidate_is_valid_after_creation(self) -> "DeterministicCandidate":
        if self.valid_until <= self.created_at:
            raise ValueError("candidate must remain valid after creation")
        return self


class ModelPolicy(FrozenModel):
    symbol: str
    action: Literal["HOLD", "ALLOW_LONG", "ALLOW_SHORT"]
    exposure: Literal["FLAT", "HALF", "FULL"]
    evidence_ids: list[str] = Field(max_length=8)
    risk_flags: list[str] = Field(max_length=8)
    invalidation_conditions: list[str] = Field(max_length=5)
    confidence_rank: int = Field(ge=0, le=100)
    valid_until: datetime

    @field_validator("valid_until")
    @classmethod
    def policy_time_is_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def hold_is_flat(self) -> "ModelPolicy":
        if self.action == "HOLD" and self.exposure != "FLAT":
            raise ValueError("HOLD policy must be FLAT")
        if self.action != "HOLD" and self.exposure == "FLAT":
            raise ValueError("an allowed policy cannot have FLAT exposure")
        return self


class PaperOrderIntent(FrozenModel):
    intent_id: str
    portfolio_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    entry_limit: float = Field(gt=0, allow_inf_nan=False)
    stop_trigger: float = Field(gt=0, allow_inf_nan=False)
    created_at: datetime
    valid_until: datetime
    candidate_packet_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_response_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("created_at", "valid_until")
    @classmethod
    def intent_times_are_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def intent_is_not_expired_at_creation(self) -> "PaperOrderIntent":
        if self.valid_until <= self.created_at:
            raise ValueError("intent must be valid after creation")
        return self


class DeterministicRiskDecision(FrozenModel):
    risk_version: Literal["baseline_risk_v1"] = "baseline_risk_v1"
    decision_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbol: str
    candidate_side: Literal["LONG_CANDIDATE", "SHORT_CANDIDATE"]
    status: Literal["APPROVED", "HOLD"]
    decided_at: datetime
    entry_estimate: float = Field(gt=0, allow_inf_nan=False)
    stop_trigger: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    quantity: int = Field(ge=0)
    reasons: list[str] = Field(min_length=1)

    @field_validator("decided_at")
    @classmethod
    def risk_decision_time_is_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def approved_risk_has_trade_parameters(self) -> "DeterministicRiskDecision":
        if self.status == "APPROVED" and (self.quantity <= 0 or self.stop_trigger is None):
            raise ValueError("approved risk decision needs quantity and stop")
        if self.status == "HOLD" and self.quantity != 0:
            raise ValueError("HOLD risk decision must have zero quantity")
        return self


class PaperFill(FrozenModel):
    fill_version: Literal["next_observable_v1"] = "next_observable_v1"
    fill_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbol: str
    action: Literal["BUY", "SELL"]
    purpose: Literal["ENTRY", "STOP", "FLATTEN"]
    filled_at: datetime
    quantity: int = Field(gt=0)
    observed_quote: float = Field(gt=0, allow_inf_nan=False)
    fill_price: float = Field(gt=0, allow_inf_nan=False)
    spread_cost: float = Field(ge=0, allow_inf_nan=False)
    slippage_cost: float = Field(ge=0, allow_inf_nan=False)
    provider_message_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    partial: bool

    @field_validator("filled_at")
    @classmethod
    def fill_time_is_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)


class PaperTradeOutcome(FrozenModel):
    outcome_version: Literal["paper_outcome_v1"] = "paper_outcome_v1"
    outcome_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbol: str
    direction: Literal["LONG", "SHORT"]
    cost_profile: str
    entry_at: datetime
    exit_at: datetime
    exit_reason: Literal["STOP", "FLATTEN"]
    quantity: int = Field(gt=0)
    entry_price: float = Field(gt=0, allow_inf_nan=False)
    exit_price: float = Field(gt=0, allow_inf_nan=False)
    initial_risk: float = Field(gt=0, allow_inf_nan=False)
    gross_pnl: float = Field(allow_inf_nan=False)
    explicit_costs: float = Field(ge=0, allow_inf_nan=False)
    spread_cost: float = Field(ge=0, allow_inf_nan=False)
    slippage_cost: float = Field(ge=0, allow_inf_nan=False)
    net_pnl: float = Field(allow_inf_nan=False)

    @field_validator("entry_at", "exit_at")
    @classmethod
    def outcome_times_are_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def outcome_time_order_is_valid(self) -> "PaperTradeOutcome":
        if self.exit_at <= self.entry_at:
            raise ValueError("paper exit must occur after paper entry")
        return self


class PaperRewardRecord(FrozenModel):
    reward_version: Literal["review_reward_v1"] = "review_reward_v1"
    reward_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    cost_profile: str
    outcome_known_at: datetime
    label: Literal["CORRECT_AFTER_COSTS", "INCORRECT_AFTER_COSTS", "BREAKEVEN"]
    reward_r_multiple: float = Field(allow_inf_nan=False)
    bonus_points: float = Field(ge=0, le=2, allow_inf_nan=False)
    mistake_tags: list[str]
    review_status: Literal["PENDING_HUMAN_REVIEW"] = "PENDING_HUMAN_REVIEW"
    eligible_for_model_memory: Literal[False] = False

    @field_validator("outcome_known_at")
    @classmethod
    def reward_time_is_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)


class MemoryAvailability(FrozenModel):
    event_observed_at: datetime
    outcome_known_at: datetime
    review_approved_at: datetime
    eligible_from: datetime
    source_ids: list[str]
    reviewer: str
    memory_version: str

    @field_validator(
        "event_observed_at",
        "outcome_known_at",
        "review_approved_at",
        "eligible_from",
    )
    @classmethod
    def memory_times_are_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def eligible_from_is_latest_timestamp(self) -> "MemoryAvailability":
        expected = max(
            self.event_observed_at,
            self.outcome_known_at,
            self.review_approved_at,
        )
        if self.eligible_from != expected:
            raise ValueError("eligible_from must equal the latest availability timestamp")
        return self


class RunCard(FrozenModel):
    run_id: str
    parent_run_id: str | None
    mode: Literal["DEVELOPMENT", "VALIDATION", "SEALED", "FORWARD_PAPER"]
    started_at: datetime
    finished_at: datetime
    code_commit: str
    dirty_worktree: bool
    config_hash: str
    prompt_hashes: dict[str, str]
    dependency_lock_hash: str
    runtime_manifest_hash: str
    model_name: str
    model_digest: str
    data_partition_hashes: list[str]
    evidence_manifest_hash: str
    experiment_id: str
    ablation_id: str
    metrics_hash: str
    report_hash: str
    previous_run_card_hash: str | None = None
    run_card_hash: str
    required_evidence: dict[str, Any]

    @field_validator("started_at", "finished_at")
    @classmethod
    def run_times_are_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def run_has_positive_duration(self) -> "RunCard":
        if self.finished_at < self.started_at:
            raise ValueError("run cannot finish before it starts")
        return self
