from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from stage1.schemas import NormalizedTick, QuarantinedMarketEvent, RawTickRecord
from stage1.secrets import redact_mapping


class MarketMessageError(ValueError):
    """Base error for provider messages that cannot enter the data spine."""


class UnsupportedMarketMessage(MarketMessageError):
    """Control/status messages are not tick records."""


class UnknownSymbolError(MarketMessageError):
    """The provider sent a symbol outside the configured universe."""


_MISSING = object()
_FYERS_CONTROL_TYPES = frozenset({"cn", "sub", "unsub", "lit", "ful", "cp", "cr"})


def _first(message: Mapping[str, Any], keys: Sequence[str], default: Any = _MISSING) -> Any:
    for key in keys:
        if key in message and message[key] is not None:
            return message[key]
    if default is not _MISSING:
        return default
    raise MarketMessageError(f"missing required market field ({', '.join(keys)})")


def _number(
    message: Mapping[str, Any],
    keys: Sequence[str],
    *,
    required: bool = False,
) -> float | None:
    raw = _first(message, keys, default=None)
    if raw in (None, ""):
        if required:
            raise MarketMessageError(f"missing required numeric field ({', '.join(keys)})")
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise MarketMessageError(f"invalid numeric market field ({', '.join(keys)})") from exc
    if not math.isfinite(value):
        raise MarketMessageError(f"non-finite market field ({', '.join(keys)})")
    return value


def _quote_side(
    message: Mapping[str, Any],
    *,
    side: str,
    price_keys: Sequence[str],
    quantity_keys: Sequence[str],
) -> tuple[float | None, float | None]:
    price = _number(message, price_keys)
    quantity = _number(message, quantity_keys)
    if price == 0:
        if quantity == 0:
            return None, None
        raise MarketMessageError(
            f"{side} price is zero without a matching zero quantity"
        )
    return price, quantity


def _provider_timestamp(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        if raw.tzinfo is None or raw.utcoffset() is None:
            raise MarketMessageError("provider datetime must include a timezone")
        return raw.astimezone(timezone.utc)
    if isinstance(raw, str):
        stripped = raw.strip()
        try:
            numeric = float(stripped)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            except ValueError as exc:
                raise MarketMessageError("invalid provider timestamp") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise MarketMessageError("provider timestamp must include a timezone")
            return parsed.astimezone(timezone.utc)
    elif isinstance(raw, (int, float)):
        numeric = float(raw)
    else:
        raise MarketMessageError("invalid provider timestamp type")

    if not math.isfinite(numeric) or numeric <= 0:
        raise MarketMessageError("invalid provider epoch timestamp")
    if numeric > 10_000_000_000:
        numeric /= 1000.0
    try:
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise MarketMessageError("provider epoch timestamp is out of range") from exc


def _canonical_message(message: Mapping[str, Any]) -> tuple[str, str]:
    safe_message = redact_mapping(message)
    canonical = json.dumps(
        safe_message,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def quarantine_fyers_message(
    message: Mapping[str, Any],
    *,
    received_at: datetime,
    reason_code: str,
    reason: str,
) -> QuarantinedMarketEvent:
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise MarketMessageError("quarantine receive timestamp must include a timezone")
    received_ts = received_at.astimezone(timezone.utc)
    canonical, payload_hash = _canonical_message(message)
    event_material = json.dumps(
        {
            "provider": "FYERS",
            "received_ts": received_ts.isoformat(),
            "reason_code": reason_code,
            "payload_hash": payload_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    event_id = hashlib.sha256(event_material.encode("utf-8")).hexdigest()
    return QuarantinedMarketEvent(
        event_id=event_id,
        received_ts=received_ts,
        reason_code=reason_code,
        reason=reason[:300],
        payload_hash=payload_hash,
        raw_message_json=canonical,
    )


def normalize_fyers_message(
    message: Mapping[str, Any],
    *,
    allowed_symbols: set[str] | frozenset[str],
    received_at: datetime | None = None,
) -> RawTickRecord:
    if not isinstance(message, Mapping):
        raise MarketMessageError("FYERS message must be a mapping")

    symbol_raw = _first(message, ("symbol",), default=None)
    if not symbol_raw:
        message_type = str(message.get("type", "")).strip().lower()
        if message_type in _FYERS_CONTROL_TYPES:
            raise UnsupportedMarketMessage(
                f"FYERS control/status message ({message_type})"
            )
        raise MarketMessageError("missing required market field (symbol)")
    symbol = str(symbol_raw).strip().upper()
    if symbol not in allowed_symbols:
        raise UnknownSymbolError(f"symbol is outside configured universe: {symbol}")

    received_ts = received_at or datetime.now(timezone.utc)
    if received_ts.tzinfo is None or received_ts.utcoffset() is None:
        raise MarketMessageError("receive timestamp must include a timezone")
    received_ts = received_ts.astimezone(timezone.utc)

    exchange_raw = _first(
        message,
        (
            "exch_feed_time",
            "exchange_timestamp",
            "exchange_ts",
            "last_traded_time",
            "timestamp",
            "tt",
        ),
    )
    exchange_ts = _provider_timestamp(exchange_raw)
    canonical, message_hash = _canonical_message(message)
    bid, bid_qty = _quote_side(
        message,
        side="bid",
        price_keys=("bid_price", "best_bid_price", "bid"),
        quantity_keys=("bid_size", "best_bid_qty", "bid_qty"),
    )
    ask, ask_qty = _quote_side(
        message,
        side="ask",
        price_keys=("ask_price", "best_ask_price", "ask"),
        quantity_keys=("ask_size", "best_ask_qty", "ask_qty"),
    )

    try:
        tick = NormalizedTick(
            symbol=symbol,
            exchange_ts=exchange_ts,
            received_ts=received_ts,
            ltp=_number(message, ("ltp", "last_price", "lp"), required=True),
            bid=bid,
            ask=ask,
            bid_qty=bid_qty,
            ask_qty=ask_qty,
            last_traded_qty=_number(message, ("last_traded_qty", "ltq")),
            cumulative_volume=_number(
                message,
                ("vol_traded_today", "volume", "cumulative_volume", "v"),
            ),
            provider_message_hash=message_hash,
        )
    except ValidationError as exc:
        raise MarketMessageError("FYERS tick failed the normalized schema") from exc
    return RawTickRecord(tick=tick, raw_message_json=canonical)
