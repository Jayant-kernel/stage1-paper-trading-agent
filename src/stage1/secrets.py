from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

_SENSITIVE_KEY = re.compile(
    r"(secret|token|password|passwd|authorization|auth_code|api[_-]?key|"
    r"client[_-]?id|chat[_-]?id)",
    re.IGNORECASE,
)
_KEY_VALUE = re.compile(
    r"(?i)\b(telegram[_-]?chat[_-]?id|fyers[_-]?client[_-]?id|"
    r"secret|token|password|passwd|authorization|auth_code|api[_-]?key|"
    r"client[_-]?id|chat[_-]?id)\b(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_CALLBACK_CODE = re.compile(r"(?i)([?&](?:auth_)?code=)[^&#\s]+")
_LOCAL_ENV_SECRET_KEYS = frozenset(
    {
        "FYERS_ACCESS_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    }
)


class FyersDataCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    client_id: SecretStr
    access_token: SecretStr

    @field_validator("client_id", "access_token")
    @classmethod
    def credential_is_not_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("FYERS data credentials cannot be blank")
        return value

    def websocket_access_token(self) -> str:
        client_id = self.client_id.get_secret_value()
        token = self.access_token.get_secret_value()
        if token.startswith(f"{client_id}:"):
            return token
        return f"{client_id}:{token}"

    def known_secret_values(self) -> tuple[str, ...]:
        return (
            self.client_id.get_secret_value(),
            self.access_token.get_secret_value(),
        )


class TelegramAlertCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bot_token: SecretStr
    chat_id: SecretStr

    @field_validator("bot_token", "chat_id")
    @classmethod
    def credential_is_not_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Telegram alert credentials cannot be blank")
        return value

    @field_validator("chat_id")
    @classmethod
    def chat_id_is_numeric(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        if not re.fullmatch(r"[0-9]+", raw) or int(raw) <= 0:
            raise ValueError("Telegram private chat ID must be a positive integer")
        return value


class SecretBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    app_env: Literal["paper"] = "paper"
    live_order_endpoints_enabled: Literal[False] = False
    fyers_client_id: SecretStr | None = None
    fyers_secret: SecretStr | None = None
    fyers_redirect_uri: str = "http://127.0.0.1:8765/callback"
    fyers_access_token: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    cerebras_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None

    @field_validator("fyers_redirect_uri")
    @classmethod
    def redirect_is_loopback_http(cls, value: str) -> str:
        allowed_prefixes = ("http://127.0.0.1:", "http://localhost:")
        if not value.startswith(allowed_prefixes):
            raise ValueError("Stage 1 FYERS redirect must use a local loopback address")
        return value

    def require_fyers_app(self) -> tuple[str, str]:
        if self.fyers_client_id is None or self.fyers_secret is None:
            raise RuntimeError("FYERS_CLIENT_ID and FYERS_SECRET are required locally")
        return (
            self.fyers_client_id.get_secret_value(),
            self.fyers_secret.get_secret_value(),
        )

    def require_fyers_data_credentials(self) -> FyersDataCredentials:
        if self.fyers_client_id is None or self.fyers_access_token is None:
            raise RuntimeError("FYERS_CLIENT_ID and FYERS_ACCESS_TOKEN are required locally")
        return FyersDataCredentials(
            client_id=self.fyers_client_id,
            access_token=self.fyers_access_token,
        )

    def require_telegram_alert_credentials(self) -> TelegramAlertCredentials:
        if self.telegram_bot_token is None or self.telegram_chat_id is None:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required locally"
            )
        return TelegramAlertCredentials(
            bot_token=self.telegram_bot_token,
            chat_id=self.telegram_chat_id,
        )

    def known_secret_values(self) -> tuple[str, ...]:
        values: list[str] = []
        for name in (
            "fyers_client_id",
            "fyers_secret",
            "fyers_access_token",
            "telegram_bot_token",
            "telegram_chat_id",
            "groq_api_key",
            "cerebras_api_key",
            "gemini_api_key",
        ):
            secret = getattr(self, name)
            if secret is not None:
                value = secret.get_secret_value()
                if value:
                    values.append(value)
        return tuple(values)


def _bool_value(raw: str | None, *, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("boolean environment value must be true or false")


def load_secrets(env_file: str | Path = ".env") -> SecretBundle:
    file_values: Mapping[str, str | None] = {}
    path = Path(env_file)
    if path.exists():
        file_values = dotenv_values(path)

    def get(name: str, default: str | None = None) -> str | None:
        value = os.environ.get(name)
        if value is None:
            value = file_values.get(name)
        return default if value is None else value

    def get_secret(name: str) -> str | None:
        value = get(name)
        if value is None or not value.strip():
            return None
        return value.strip()

    app_env = (get("APP_ENV", "paper") or "paper").strip().lower()
    live_enabled = _bool_value(
        get("LIVE_ORDER_ENDPOINTS_ENABLED"),
        default=False,
    )
    if app_env != "paper":
        raise ValueError("Stage 1 APP_ENV must be paper")
    if live_enabled:
        raise ValueError("Stage 1 forbids live order endpoints")

    return SecretBundle(
        app_env="paper",
        live_order_endpoints_enabled=False,
        fyers_client_id=get_secret("FYERS_CLIENT_ID"),
        fyers_secret=get_secret("FYERS_SECRET"),
        fyers_redirect_uri=get(
            "FYERS_REDIRECT_URI",
            "http://127.0.0.1:8765/callback",
        ),
        fyers_access_token=get_secret("FYERS_ACCESS_TOKEN"),
        telegram_bot_token=get_secret("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=get_secret("TELEGRAM_CHAT_ID"),
        groq_api_key=get_secret("GROQ_API_KEY"),
        cerebras_api_key=get_secret("CEREBRAS_API_KEY"),
        gemini_api_key=get_secret("GEMINI_API_KEY"),
    )


def redact_text(text: str, *, secret_values: tuple[str, ...] = ()) -> str:
    redacted = text
    for value in sorted((item for item in secret_values if item), key=len, reverse=True):
        redacted = redacted.replace(value, "[REDACTED]")
    redacted = _BEARER.sub("Bearer [REDACTED]", redacted)
    redacted = _CALLBACK_CODE.sub(r"\1[REDACTED]", redacted)
    redacted = _KEY_VALUE.sub(r"\1\2[REDACTED]", redacted)
    return redacted


def redact_mapping(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if _SENSITIVE_KEY.search(str(key))
                else redact_mapping(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_mapping(item) for item in value)
    return value


def secret_presence(bundle: SecretBundle) -> dict[str, bool]:
    return {
        "fyers_client_id": bundle.fyers_client_id is not None,
        "fyers_secret": bundle.fyers_secret is not None,
        "fyers_access_token": bundle.fyers_access_token is not None,
        "telegram_bot_token": bundle.telegram_bot_token is not None,
        "telegram_chat_id": bundle.telegram_chat_id is not None,
    }


def store_local_env_values(env_path: str | Path, values: Mapping[str, str]) -> None:
    """Atomically update the untracked local .env without logging values."""

    path = Path(env_path)
    if path.name != ".env":
        raise ValueError("local secret values may only be written to a .env file")
    if path.is_symlink():
        raise ValueError("refusing to write secrets through a symbolic link")
    if not values:
        raise ValueError("at least one local secret value is required")
    unknown = set(values) - _LOCAL_ENV_SECRET_KEYS
    if unknown:
        raise ValueError("unsupported local secret key")
    for value in values.values():
        if not value or "\n" in value or "\r" in value:
            raise ValueError("local secret values must be non-empty single lines")

    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    target_keys = set(values)
    written: set[str] = set()
    output: list[str] = []
    for line in existing.splitlines():
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in target_keys:
            if key not in written:
                output.append(f"{key}={values[key]}")
                written.add(key)
        else:
            output.append(line)
    pending = [(key, value) for key, value in values.items() if key not in written]
    if pending and output and output[-1] != "":
        output.append("")
    output.extend(f"{key}={value}" for key, value in pending)

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.replace(temporary, path)
