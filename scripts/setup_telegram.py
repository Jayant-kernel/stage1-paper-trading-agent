from __future__ import annotations

import getpass
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stage1.alerts.telegram import (  # noqa: E402
    TelegramAlertError,
    TelegramOutboundClient,
)
from stage1.secrets import (  # noqa: E402
    TelegramAlertCredentials,
    store_local_env_values,
)

_BOT_API_ORIGIN = "https://api.telegram.org"
_SETUP_METHODS = frozenset({"getMe", "deleteWebhook", "getUpdates"})
_TEST_MESSAGE = "Stage 1 paper agent alerts connected."


class TelegramSetupError(RuntimeError):
    """A setup failure whose text is safe to display without credentials."""


def _setup_api_call(
    client: httpx.Client,
    token: str,
    method: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    if method not in _SETUP_METHODS:
        raise TelegramSetupError("unsupported Telegram setup method")
    try:
        response = client.post(
            f"{_BOT_API_ORIGIN}/bot{token}/{method}",
            json=payload or {},
        )
        body = response.json()
    except (httpx.HTTPError, ValueError):
        raise TelegramSetupError("Telegram setup request failed") from None
    if (
        response.status_code != 200
        or not isinstance(body, dict)
        or body.get("ok") is not True
    ):
        raise TelegramSetupError("Telegram rejected the setup request")
    return body.get("result")


def _private_start_candidates(updates: Any) -> dict[str, int]:
    candidates: dict[str, int] = {}
    if not isinstance(updates, list):
        raise TelegramSetupError("Telegram returned an invalid updates response")
    for update in updates:
        if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
            continue
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        text = message.get("text")
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(text, str) or not isinstance(chat, dict):
            continue
        if text != "/start" and not text.startswith("/start "):
            continue
        if chat.get("type") != "private" or not isinstance(chat.get("id"), int):
            continue
        if not isinstance(sender, dict) or sender.get("is_bot") is True:
            continue
        if sender.get("id") != chat.get("id"):
            continue
        chat_id = str(chat["id"])
        candidates[chat_id] = max(candidates.get(chat_id, -1), update["update_id"])
    return candidates


def detect_private_chat_id(
    client: httpx.Client,
    token: str,
    *,
    wait_seconds: float = 90.0,
) -> tuple[str, int]:
    deadline = time.monotonic() + wait_seconds
    offset: int | None = None
    while time.monotonic() < deadline:
        request: dict[str, Any] = {
            "limit": 100,
            "timeout": min(10, max(1, int(deadline - time.monotonic()))),
            "allowed_updates": ["message"],
        }
        if offset is not None:
            request["offset"] = offset
        updates = _setup_api_call(client, token, "getUpdates", request)
        candidates = _private_start_candidates(updates)
        if len(candidates) > 1:
            raise TelegramSetupError(
                "multiple private /start chats were found; refusing to choose one"
            )
        if len(candidates) == 1:
            chat_id, update_id = next(iter(candidates.items()))
            return chat_id, update_id
        if isinstance(updates, list) and updates:
            valid_ids = [
                item["update_id"]
                for item in updates
                if isinstance(item, dict) and isinstance(item.get("update_id"), int)
            ]
            if valid_ids:
                offset = max(valid_ids) + 1
    raise TelegramSetupError(
        "no private /start message was found; send /start to the bot and run setup again"
    )


def run_setup(
    *,
    token: str,
    env_path: Path,
    client: httpx.Client,
) -> None:
    if not token or any(character.isspace() for character in token):
        raise TelegramSetupError("the Telegram bot token format is invalid")
    identity = _setup_api_call(client, token, "getMe")
    if not isinstance(identity, dict) or identity.get("is_bot") is not True:
        raise TelegramSetupError("the supplied Telegram token is not a bot token")

    _setup_api_call(
        client,
        token,
        "deleteWebhook",
        {"drop_pending_updates": False},
    )
    chat_id, update_id = detect_private_chat_id(client, token)
    credentials = TelegramAlertCredentials(bot_token=token, chat_id=chat_id)
    try:
        TelegramOutboundClient(credentials, client=client).send_message(_TEST_MESSAGE)
    except TelegramAlertError:
        raise TelegramSetupError("the outbound Telegram test message failed") from None

    _setup_api_call(
        client,
        token,
        "getUpdates",
        {
            "offset": update_id + 1,
            "limit": 1,
            "timeout": 0,
            "allowed_updates": ["message"],
        },
    )
    store_local_env_values(
        env_path,
        {
            "TELEGRAM_BOT_TOKEN": token,
            "TELEGRAM_CHAT_ID": chat_id,
        },
    )


def main() -> int:
    print("Telegram setup uses a hidden local token prompt.")
    token = getpass.getpass("Telegram bot token (hidden): ").strip()
    print("Looking for one private /start message...")
    try:
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            run_setup(token=token, env_path=ROOT / ".env", client=client)
    except TelegramSetupError as exc:
        print(f"Setup stopped: {exc}", file=sys.stderr)
        return 1
    except ValueError:
        print("Setup stopped: local credential validation failed", file=sys.stderr)
        return 1
    finally:
        token = ""
    print("Telegram outbound alerts are configured and the test message was sent.")
    print("Runtime command polling and webhooks remain disabled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
