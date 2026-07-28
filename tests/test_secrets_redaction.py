from pathlib import Path

import pytest

from stage1.secrets import (
    SecretBundle,
    TelegramAlertCredentials,
    load_secrets,
    redact_mapping,
    redact_text,
    secret_presence,
    store_local_env_values,
)


def test_secret_bundle_serialization_never_exposes_values() -> None:
    raw_secret = "super-secret-fyers-value"
    bundle = SecretBundle(
        fyers_client_id="CLIENT-100",
        fyers_secret=raw_secret,
        fyers_access_token="opaque-access-token",
    )
    dumped = bundle.model_dump_json()
    assert raw_secret not in dumped
    assert "opaque-access-token" not in dumped
    assert "**********" in dumped


def test_market_data_gateway_receives_only_narrow_fyers_credentials() -> None:
    bundle = SecretBundle(
        fyers_client_id="CLIENT-100",
        fyers_secret="app-secret-not-needed-by-recorder",
        fyers_access_token="opaque-access-token",
        telegram_bot_token="telegram-secret",
    )
    credentials = bundle.require_fyers_data_credentials()
    serialized = credentials.model_dump_json()
    assert "app-secret-not-needed-by-recorder" not in serialized
    assert "telegram-secret" not in serialized
    assert "opaque-access-token" not in serialized
    assert credentials.websocket_access_token() == "CLIENT-100:opaque-access-token"


def test_diagnostic_redaction_masks_known_and_structured_secrets() -> None:
    text = (
        "token=abc123 Authorization: Bearer ey.fake.jwt "
        "https://localhost/callback?auth_code=private-code&state=ok "
        "TELEGRAM_CHAT_ID=24680 "
        "literal-secret"
    )
    redacted = redact_text(text, secret_values=("literal-secret",))
    for forbidden in (
        "abc123",
        "ey.fake.jwt",
        "private-code",
        "24680",
        "literal-secret",
    ):
        assert forbidden not in redacted
    assert redacted.count("[REDACTED]") >= 4


def test_mapping_redaction_is_recursive() -> None:
    raw = {
        "symbol": "NSE:TCS-EQ",
        "access_token": "never-persist",
        "chat_id": "never-log-chat",
        "nested": {"apiKey": "never-log", "ltp": 100.0},
    }
    safe = redact_mapping(raw)
    assert safe["access_token"] == "[REDACTED]"
    assert safe["chat_id"] == "[REDACTED]"
    assert safe["nested"]["apiKey"] == "[REDACTED]"
    assert safe["nested"]["ltp"] == 100.0


def test_loader_reports_presence_only_and_rejects_live_mode(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENV=paper\n"
        "LIVE_ORDER_ENDPOINTS_ENABLED=false\n"
        "FYERS_CLIENT_ID=local-client\n",
        encoding="utf-8",
    )
    presence = secret_presence(load_secrets(env_file))
    assert presence["fyers_client_id"] is True
    assert presence["fyers_secret"] is False

    env_file.write_text(
        "APP_ENV=paper\nLIVE_ORDER_ENDPOINTS_ENABLED=true\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="forbids live"):
        load_secrets(env_file)


def test_blank_env_secrets_are_treated_as_missing(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENV=paper\n"
        "LIVE_ORDER_ENDPOINTS_ENABLED=false\n"
        "FYERS_CLIENT_ID=   \n"
        "FYERS_SECRET=\n"
        "FYERS_ACCESS_TOKEN=\n",
        encoding="utf-8",
    )

    bundle = load_secrets(env_file)

    assert bundle.fyers_client_id is None
    assert bundle.fyers_secret is None
    assert bundle.fyers_access_token is None
    with pytest.raises(RuntimeError, match="FYERS_CLIENT_ID and FYERS_SECRET"):
        bundle.require_fyers_app()


def test_data_credentials_reject_blank_values() -> None:
    with pytest.raises(ValueError, match="cannot be blank"):
        SecretBundle(
            fyers_client_id=" ",
            fyers_access_token="token",
        ).require_fyers_data_credentials()


def test_telegram_credentials_are_narrow_and_redacted() -> None:
    bundle = SecretBundle(
        telegram_bot_token="local-bot-token",
        telegram_chat_id="24680",
        fyers_secret="not-for-alert-client",
    )

    credentials = bundle.require_telegram_alert_credentials()

    serialized = credentials.model_dump_json()
    assert "local-bot-token" not in serialized
    assert "24680" not in serialized
    assert "not-for-alert-client" not in serialized
    assert TelegramAlertCredentials(bot_token="token", chat_id="24680")


def test_local_secret_storage_is_atomic_env_only_and_removes_duplicates(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENV=paper\n"
        "TELEGRAM_BOT_TOKEN=old-one\n"
        "TELEGRAM_BOT_TOKEN=old-two\n"
        "TELEGRAM_CHAT_ID=1\n",
        encoding="utf-8",
    )

    store_local_env_values(
        env_file,
        {
            "TELEGRAM_BOT_TOKEN": "new-token",
            "TELEGRAM_CHAT_ID": "24680",
        },
    )

    stored = env_file.read_text(encoding="utf-8")
    assert stored.count("TELEGRAM_BOT_TOKEN=") == 1
    assert "TELEGRAM_BOT_TOKEN=new-token" in stored
    assert "TELEGRAM_CHAT_ID=24680" in stored
    assert "APP_ENV=paper" in stored
    assert not (tmp_path / ".env.tmp").exists()

    with pytest.raises(ValueError, match="only be written to a .env"):
        store_local_env_values(tmp_path / "credentials.txt", {"TELEGRAM_CHAT_ID": "2"})
