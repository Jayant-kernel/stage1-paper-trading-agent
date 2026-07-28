import hashlib
from datetime import datetime, timedelta, timezone

import polars as pl

from stage1.config import load_config
from stage1.evaluation.intraday_lab import (
    apply_artificial_delay,
    load_intraday_protocol,
)

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def test_frozen_protocol_respects_stage1_limits_and_keeps_test_sealed() -> None:
    stage1 = load_config(ROOT / "config" / "stage1.yaml")
    protocol = load_intraday_protocol(
        ROOT / "config" / "intraday_variants.yaml", stage1=stage1
    )

    assert protocol.test_window == "SEALED"
    assert len(protocol.variants) == 6
    assert {session.role for session in protocol.sessions} == {
        "DEVELOPMENT",
        "VALIDATION",
    }
    assert any("ablation" in variant.id for variant in protocol.variants)


def test_artificial_delay_changes_identity_without_extending_validity() -> None:
    created = datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc)
    original_id = hashlib.sha256(b"candidate").hexdigest()
    frame = pl.DataFrame(
        [
            {
                "candidate_version": "baseline_v1",
                "candidate_id": original_id,
                "symbol": "NSE:SBIN-EQ",
                "side": "LONG_CANDIDATE",
                "created_at": created,
                "valid_until": created + timedelta(minutes=3),
                "score": 0.5,
                "spread_bps": 3.0,
                "relative_return_5m_bps": 20.0,
                "snapshot_hash": hashlib.sha256(b"snapshot").hexdigest(),
                "deterministic_reasons": ["fixture"],
            }
        ]
    )

    delayed = apply_artificial_delay(frame, 15)

    assert delayed["candidate_id"][0] != original_id
    assert delayed["created_at"][0] == created + timedelta(seconds=15)
    assert delayed["valid_until"][0] == frame["valid_until"][0]
    assert delayed["deterministic_reasons"][0][-1] == "artificial_delay_seconds:15"
