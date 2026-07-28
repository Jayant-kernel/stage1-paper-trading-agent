from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from stage1.config import Stage1Config, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_authoritative_config_is_paper_only_and_has_fixed_universe() -> None:
    config = load_config(ROOT / "config" / "stage1.yaml")
    assert config.mode == "paper"
    assert config.live_order_endpoints_enabled is False
    assert len(config.universe.symbols) == 10
    assert len(config.universe.context_symbols) == 2
    assert len(config.universe.all_symbols) == 12
    assert config.models.cloud_shadow.enabled is False
    assert config.data.stale_tick_seconds == 6


def test_live_endpoint_flag_is_schema_invalid() -> None:
    raw = yaml.safe_load((ROOT / "config" / "stage1.yaml").read_text(encoding="utf-8"))
    raw["live_order_endpoints_enabled"] = True
    with pytest.raises(ValidationError):
        Stage1Config.model_validate(raw)


def test_cloud_shadow_cannot_be_enabled_initially() -> None:
    raw = yaml.safe_load((ROOT / "config" / "stage1.yaml").read_text(encoding="utf-8"))
    raw["models"]["cloud_shadow"]["enabled"] = True
    with pytest.raises(ValidationError):
        Stage1Config.model_validate(raw)


def test_frozen_v1_universe_cannot_be_silently_reselected() -> None:
    raw = yaml.safe_load((ROOT / "config" / "stage1.yaml").read_text(encoding="utf-8"))
    raw["universe"]["symbols"][0] = "NSE:OTHER-EQ"
    with pytest.raises(ValidationError, match="frozen Stage 1 v1 universe"):
        Stage1Config.model_validate(raw)
