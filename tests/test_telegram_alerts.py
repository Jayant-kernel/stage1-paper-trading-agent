import json
from pathlib import Path

import httpx
import pytest

from scripts.setup_telegram import TelegramSetupError, run_setup
from stage1.alerts.telegram import TelegramAlertError, TelegramOutboundClient
from stage1.secrets import TelegramAlertCredentials

ROOT = Path(__file__).resolve().parents[1]


def _token() -> str:
    return str(123_456_789) + ":" + ("A" * 35)


def test_runtime_telegram_client_can_only_send_messages() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 77}},
        )

    credentials = TelegramAlertCredentials(bot_token=_token(), chat_id="24680")
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramOutboundClient(credentials, client=http_client)
        assert client.send_message("paper alert") == 77

    assert len(requests) == 1
    assert requests[0].url.path.endswith("/sendMessage")
    assert json.loads(requests[0].content) == {
        "chat_id": "24680",
        "text": "paper alert",
    }


def test_runtime_telegram_failure_does_not_expose_token() -> None:
    token = _token()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": token})

    credentials = TelegramAlertCredentials(bot_token=token, chat_id="24680")
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramOutboundClient(credentials, client=http_client)
        with pytest.raises(TelegramAlertError) as error:
            client.send_message("paper alert")
    assert token not in str(error.value)


def test_one_time_setup_detects_private_start_sends_and_stores(tmp_path: Path) -> None:
    token = _token()
    calls: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        calls.append((method, body))
        if method == "getMe":
            result: object = {"id": 99, "is_bot": True, "username": "paper_alert_bot"}
        elif method == "deleteWebhook":
            result = True
        elif method == "getUpdates" and body.get("offset") == 43:
            result = []
        elif method == "getUpdates":
            result = [
                {
                    "update_id": 42,
                    "message": {
                        "text": "/start",
                        "chat": {"id": 24680, "type": "private"},
                        "from": {"id": 24680, "is_bot": False},
                    },
                }
            ]
        elif method == "sendMessage":
            result = {"message_id": 88}
        else:
            return httpx.Response(404, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "result": result})

    env_file = tmp_path / ".env"
    env_file.write_text("APP_ENV=paper\nFYERS_ACCESS_TOKEN=keep-me\n", encoding="utf-8")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run_setup(token=token, env_path=env_file, client=client)

    stored = env_file.read_text(encoding="utf-8")
    assert f"TELEGRAM_BOT_TOKEN={token}" in stored
    assert "TELEGRAM_CHAT_ID=24680" in stored
    assert "FYERS_ACCESS_TOKEN=keep-me" in stored
    assert ("deleteWebhook", {"drop_pending_updates": False}) in calls
    assert (
        "sendMessage",
        {
            "chat_id": "24680",
            "text": "Stage 1 paper agent alerts connected.",
        },
    ) in calls
    assert any(method == "getUpdates" and body.get("offset") == 43 for method, body in calls)


def test_setup_refuses_ambiguous_private_start_chats(tmp_path: Path) -> None:
    token = _token()

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            result: object = {"id": 99, "is_bot": True}
        elif method == "deleteWebhook":
            result = True
        else:
            result = [
                {
                    "update_id": update_id,
                    "message": {
                        "text": "/start",
                        "chat": {"id": chat_id, "type": "private"},
                        "from": {"id": chat_id, "is_bot": False},
                    },
                }
                for update_id, chat_id in ((1, 111), (2, 222))
            ]
        return httpx.Response(200, json={"ok": True, "result": result})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TelegramSetupError, match="multiple private"):
            run_setup(token=token, env_path=tmp_path / ".env", client=client)
    assert not (tmp_path / ".env").exists()


def test_runtime_alert_source_has_no_inbound_command_surface() -> None:
    runtime_source = (ROOT / "src" / "stage1" / "alerts" / "telegram.py").read_text(
        encoding="utf-8"
    )
    forbidden = (
        "getUpdates",
        "setWebhook",
        "CommandHandler",
        "MessageHandler",
        "run_polling",
        "start_polling",
        "webhook",
    )
    assert not [marker for marker in forbidden if marker in runtime_source]

    setup_source = (ROOT / "scripts" / "setup_telegram.py").read_text(encoding="utf-8")
    powershell_source = (ROOT / "scripts" / "setup_telegram.ps1").read_text(
        encoding="utf-8"
    )
    assert "getpass.getpass" in setup_source
    assert "param()" in powershell_source
