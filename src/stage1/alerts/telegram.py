from __future__ import annotations

from typing import Any

import httpx

from stage1.secrets import TelegramAlertCredentials

_BOT_API_ORIGIN = "https://api.telegram.org"


class TelegramAlertError(RuntimeError):
    """A redacted failure from the outbound-only Telegram boundary."""


class TelegramOutboundClient:
    """Send-only Telegram alerts; this client has no update or command API."""

    def __init__(
        self,
        credentials: TelegramAlertCredentials,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._credentials = credentials
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=20.0, follow_redirects=False)

    def send_message(self, text: str) -> int:
        if not text or len(text) > 4096:
            raise ValueError("Telegram alert text must contain 1 to 4096 characters")
        token = self._credentials.bot_token.get_secret_value()
        chat_id = self._credentials.chat_id.get_secret_value()
        try:
            response = self._client.post(
                f"{_BOT_API_ORIGIN}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramAlertError("Telegram sendMessage request failed") from None
        if (
            response.status_code != 200
            or not isinstance(payload, dict)
            or payload.get("ok") is not True
            or not isinstance(payload.get("result"), dict)
        ):
            raise TelegramAlertError("Telegram rejected the outbound alert")
        message_id = payload["result"].get("message_id")
        if not isinstance(message_id, int):
            raise TelegramAlertError("Telegram returned an invalid sendMessage response")
        return message_id

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "TelegramOutboundClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
