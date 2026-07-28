from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MarketConfig(StrictModel):
    open: time
    candidate_start: time
    new_entry_cutoff: time
    flatten: time
    shutdown: time

    @model_validator(mode="after")
    def times_are_ordered(self) -> "MarketConfig":
        ordered = (
            self.open,
            self.candidate_start,
            self.new_entry_cutoff,
            self.flatten,
            self.shutdown,
        )
        if tuple(sorted(ordered)) != ordered or len(set(ordered)) != len(ordered):
            raise ValueError("market times must be strictly chronological")
        return self


class DataConfig(StrictModel):
    provider: Literal["fyers"]
    bar_interval_seconds: int = Field(ge=1)
    stale_tick_seconds: int = Field(ge=1)
    reconnect_backoff_seconds: list[int] = Field(min_length=1)
    raw_tick_retention_days: int = Field(ge=1)
    parquet_batch_size: int = Field(ge=1, le=100_000)
    parquet_flush_seconds: int = Field(ge=1, le=300)

    @field_validator("reconnect_backoff_seconds")
    @classmethod
    def backoff_is_positive(cls, values: list[int]) -> list[int]:
        if any(value <= 0 for value in values):
            raise ValueError("all reconnect delays must be positive")
        return values


class UniverseConfig(StrictModel):
    symbols: list[str]
    context_symbols: list[str]

    @model_validator(mode="after")
    def fixed_universe_is_well_formed(self) -> "UniverseConfig":
        if len(self.symbols) != 10:
            raise ValueError("Stage 1 requires exactly ten trade symbols")
        if len(self.context_symbols) != 2:
            raise ValueError("Stage 1 requires NIFTY 50 and India VIX context symbols")
        all_symbols = self.symbols + self.context_symbols
        if len(set(all_symbols)) != len(all_symbols):
            raise ValueError("universe symbols must be unique")
        if any(not symbol.startswith("NSE:") for symbol in all_symbols):
            raise ValueError("Stage 1 universe is NSE-only")
        expected_trade_symbols = {
            "NSE:RELIANCE-EQ",
            "NSE:TCS-EQ",
            "NSE:INFY-EQ",
            "NSE:HDFCBANK-EQ",
            "NSE:ICICIBANK-EQ",
            "NSE:SBIN-EQ",
            "NSE:BHARTIARTL-EQ",
            "NSE:LT-EQ",
            "NSE:AXISBANK-EQ",
            "NSE:ITC-EQ",
        }
        if set(self.symbols) != expected_trade_symbols:
            raise ValueError("trade symbols must match the frozen Stage 1 v1 universe")
        expected_context = {"NSE:NIFTY50-INDEX", "NSE:INDIAVIX-INDEX"}
        if set(self.context_symbols) != expected_context:
            raise ValueError("context symbols must be NIFTY 50 and India VIX")
        return self

    @property
    def all_symbols(self) -> tuple[str, ...]:
        return tuple(self.symbols + self.context_symbols)


class CandidateGateConfig(StrictModel):
    cadence_minutes: int = Field(ge=1)
    max_candidates: int = Field(ge=0, le=10)
    min_price: float = Field(gt=0)
    max_estimated_spread_bps: float = Field(gt=0)
    min_volume_z: float
    cooldown_minutes_after_decision: int = Field(ge=0)


class LocalModelConfig(StrictModel):
    name: str
    digest: str
    quantization: Literal["Q4_K_M"]
    base_url: str
    context_tokens: int = Field(ge=1024)
    temperature: float = Field(ge=0, le=1)
    max_output_tokens: int = Field(ge=1)
    timeout_seconds: int = Field(ge=1)


class CloudShadowConfig(StrictModel):
    enabled: bool
    max_batch_gates_per_day: int = Field(ge=0)
    providers: list[Literal["groq", "cerebras", "gemini"]]
    timeout_seconds: int = Field(ge=1)


class ModelsConfig(StrictModel):
    local: LocalModelConfig
    cloud_shadow: CloudShadowConfig


class PaperConfig(StrictModel):
    starting_cash: float = Field(gt=0)
    max_open_positions: int = Field(ge=0)
    max_round_trips_per_day: int = Field(ge=0)
    risk_per_trade_fraction: float = Field(gt=0, lt=1)
    max_daily_loss_fraction: float = Field(gt=0, lt=1)
    max_position_fraction: float = Field(gt=0, le=1)
    max_sector_positions: int = Field(ge=0)
    cost_profiles: list[Literal["shoonya", "zerodha"]]
    fill_deadline_seconds: int = Field(ge=1)
    default_slippage_bps: float = Field(ge=0)
    fallback_half_spread_bps: float = Field(ge=0)
    tick_size: float = Field(gt=0)
    stop_atr_multiple: float = Field(gt=0)
    max_entry_move_atr: float = Field(gt=0)
    unknown_depth_volume_fraction: float = Field(gt=0, le=0.1)


class NewsConfig(StrictModel):
    nse_poll_seconds: int = Field(ge=1)
    sebi_rbi_poll_seconds: int = Field(ge=1)
    gdelt_enabled: bool
    max_event_age_minutes: int = Field(ge=0)
    max_items_per_symbol: int = Field(ge=0)


class AlertsConfig(StrictModel):
    telegram_enabled: bool
    outbound_only: Literal[True]


class OperationsConfig(StrictModel):
    runtime_code_read_only: Literal[True]
    halt_file: str
    heartbeat_seconds: int = Field(ge=1)
    watchdog_stale_seconds: int = Field(ge=1)
    manifest_check_seconds: int = Field(ge=1)


class EvaluationConfig(StrictModel):
    max_declared_variants: int = Field(ge=1)
    sealed_window_single_open: Literal[True]
    require_component_ablations: Literal[True]
    artificial_delay_seconds: list[float]
    memory_max_cases: int = Field(ge=0)
    require_memory_eligible_before_cutoff: Literal[True]


class Stage1Config(StrictModel):
    mode: Literal["paper"]
    timezone: Literal["Asia/Kolkata"]
    live_order_endpoints_enabled: Literal[False]
    market: MarketConfig
    data: DataConfig
    universe: UniverseConfig
    candidate_gate: CandidateGateConfig
    models: ModelsConfig
    paper: PaperConfig
    news: NewsConfig
    alerts: AlertsConfig
    operations: OperationsConfig
    evaluation: EvaluationConfig

    @model_validator(mode="after")
    def cloud_cannot_be_enabled_in_initial_config(self) -> "Stage1Config":
        if self.models.cloud_shadow.enabled:
            raise ValueError("cloud challengers remain disabled until later acceptance gates")
        return self


def load_config(path: str | Path) -> Stage1Config:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path} must contain a YAML mapping")
    return Stage1Config.model_validate(raw)
