from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BrokerageProfile(_StrictModel):
    brokerage_rate: Decimal = Field(ge=0)
    brokerage_cap_per_order: Decimal = Field(ge=0)


class StatutoryCharges(_StrictModel):
    stt_sell_rate: Decimal = Field(ge=0)
    nse_transaction_rate: Decimal = Field(ge=0)
    sebi_turnover_rate: Decimal = Field(ge=0)
    gst_rate: Decimal = Field(ge=0)
    stamp_duty_buy_rate: Decimal = Field(ge=0)


class CostTable(_StrictModel):
    version: str
    verified_at: str
    currency: str
    profiles: dict[str, BrokerageProfile]
    statutory_charges: StatutoryCharges
    sources: dict[str, str]


@dataclass(frozen=True)
class TradeCharges:
    cost_version: str
    profile: str
    brokerage: float
    stt: float
    exchange_charge: float
    sebi_charge: float
    gst: float
    stamp_duty: float
    total: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_cost_table(path: str | Path) -> CostTable:
    with Path(path).open("r", encoding="utf-8") as handle:
        return CostTable.model_validate(yaml.safe_load(handle))


def calculate_intraday_charges(
    *,
    table: CostTable,
    profile_name: str,
    buy_turnover: float,
    sell_turnover: float,
) -> TradeCharges:
    profile = table.profiles[profile_name]
    buy = Decimal(str(buy_turnover))
    sell = Decimal(str(sell_turnover))
    turnover = buy + sell
    brokerage = min(
        buy * profile.brokerage_rate,
        profile.brokerage_cap_per_order,
    ) + min(
        sell * profile.brokerage_rate,
        profile.brokerage_cap_per_order,
    )
    statutory = table.statutory_charges
    # STT is rounded to the nearest rupee on the sell-side assessment.
    stt = (sell * statutory.stt_sell_rate).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    exchange = turnover * statutory.nse_transaction_rate
    sebi = turnover * statutory.sebi_turnover_rate
    gst = (brokerage + exchange + sebi) * statutory.gst_rate
    stamp = buy * statutory.stamp_duty_buy_rate
    total = brokerage + stt + exchange + sebi + gst + stamp

    def money(value: Decimal) -> float:
        return float(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

    return TradeCharges(
        cost_version=table.version,
        profile=profile_name,
        brokerage=money(brokerage),
        stt=money(stt),
        exchange_charge=money(exchange),
        sebi_charge=money(sebi),
        gst=money(gst),
        stamp_duty=money(stamp),
        total=money(total),
    )
