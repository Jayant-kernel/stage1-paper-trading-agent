from __future__ import annotations

import json

from scripts.run_control_room import _build_live_target, _safe_event, build_snapshot


def test_control_room_event_whitelist_strips_sensitive_and_raw_fields() -> None:
    safe = _safe_event(
        {
            "event_id": "event-1234567890",
            "event_type": "fills",
            "recorded_at": "2026-07-24T05:01:29+00:00",
            "payload": {
                "symbol": "NSE:SBIN-EQ",
                "quantity": 10,
                "fill_price": 100.0,
                "raw_message_json": '{"access_token":"never-expose"}',
                "provider_message_hash": "internal-hash",
                "access_token": "never-expose",
            },
        }
    )

    serialized = json.dumps(safe)
    assert "NSE:SBIN-EQ" in serialized
    assert "never-expose" not in serialized
    assert "raw_message_json" not in serialized
    assert "provider_message_hash" not in serialized
    assert "access_token" not in serialized


def test_control_room_snapshot_preserves_paper_only_boundary() -> None:
    snapshot = build_snapshot()

    assert snapshot["safety"]["paperOnly"] is True
    assert snapshot["safety"]["liveOrderEndpointsEnabled"] is False
    assert snapshot["safety"]["brokerOrdersPossible"] is False
    assert len(snapshot["symbols"]) == 12
    assert snapshot["universe"]["tradableCount"] == 10
    assert snapshot["universe"]["contextCount"] == 2
    assert snapshot["limits"]["maxOpenPositions"] == 2
    assert snapshot["limits"]["maxRoundTripsPerDay"] == 2
    assert snapshot["target"]["simulatedExposure"] >= 0
    serialized = json.dumps(snapshot)
    assert "FYERS_ACCESS_TOKEN" not in serialized
    assert "TELEGRAM_BOT_TOKEN" not in serialized


def test_live_target_reports_exact_approved_paper_sizing() -> None:
    candidate_id = "a" * 64
    events = [
        {
            "event_type": "candidates",
            "recorded_at": "2026-07-27T04:05:00+00:00",
            "payload": {
                "candidate_id": candidate_id,
                "symbol": "NSE:SBIN-EQ",
                "side": "LONG_CANDIDATE",
                "score": 0.81,
                "created_at": "2026-07-27T04:05:00+00:00",
            },
        },
        {
            "event_type": "risk_decisions",
            "recorded_at": "2026-07-27T04:05:01+00:00",
            "payload": {
                "candidate_id": candidate_id,
                "symbol": "NSE:SBIN-EQ",
                "candidate_side": "LONG_CANDIDATE",
                "status": "APPROVED",
                "entry_estimate": 1000.0,
                "stop_trigger": 995.0,
                "quantity": 20,
                "reasons": ["all_baseline_risk_checks_passed"],
            },
        },
    ]

    target = _build_live_target(
        events,
        market_live=True,
        recorder_status="LIVE",
        engine_status="LIVE",
    )

    assert target["symbol"] == "NSE:SBIN-EQ"
    assert target["direction"] == "LONG"
    assert target["quantity"] == 20
    assert target["simulatedExposure"] == 20000.0
    assert target["riskAtStop"] == 100.0
    assert target["fillStatus"] == "NO_FILL"


def test_live_target_fails_closed_when_feed_is_not_current() -> None:
    target = _build_live_target(
        [],
        market_live=True,
        recorder_status="INTERRUPTED",
        engine_status="LIVE",
    )

    assert target["mode"] == "FEED_HOLD"
    assert target["quantity"] == 0
    assert target["simulatedExposure"] == 0.0
