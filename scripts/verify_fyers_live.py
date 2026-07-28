from __future__ import annotations

import base64
import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stage1.config import load_config  # noqa: E402
from stage1.secrets import load_secrets, redact_text  # noqa: E402


def _summary(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {
            "ok": False,
            "code": None,
            "message": f"unexpected response type: {type(response).__name__}",
        }
    status = str(response.get("s", "")).lower()
    code = response.get("code")
    message = response.get("message", "")
    return {
        "ok": status == "ok" or code in {200, 0},
        "code": code,
        "message": str(message),
    }


def _jwt_claims(token: str) -> dict[str, Any]:
    compact = token.split(":", 1)[-1]
    parts = compact.split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded)
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _token_matches_app(token: str, client_id: str) -> bool | None:
    claims = _jwt_claims(token)
    expected = client_id.strip()
    expected_base = expected.removesuffix("-100")
    candidates = [
        claims.get(name)
        for name in ("app_id", "appid", "client_id", "clientId")
        if claims.get(name)
    ]
    if not candidates:
        return None
    return any(
        str(value).strip() in {expected, expected_base}
        for value in candidates
    )


def main() -> int:
    config = load_config(ROOT / "config" / "stage1.yaml")
    if config.mode != "paper" or config.live_order_endpoints_enabled:
        raise SystemExit("paper-only configuration check failed")

    env_path = ROOT / ".env"
    bundle = load_secrets(env_path)
    credentials = bundle.require_fyers_data_credentials()
    client_id = credentials.client_id.get_secret_value()
    token = credentials.access_token.get_secret_value()

    from fyers_apiv3 import fyersModel

    sdk_log_path = ROOT / "state" / "fyers-verification-sdk"
    sdk_log_path.mkdir(parents=True, exist_ok=True)
    client = fyersModel.FyersModel(
        client_id=client_id,
        token=token,
        is_async=False,
        log_path=str(sdk_log_path),
        log_level="ERROR",
    )

    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=7)
    calls = {
        "profile": lambda: client.get_profile(),
        "quotes": lambda: client.quotes({"symbols": "NSE:SBIN-EQ"}),
        "history": lambda: client.history(
            {
                "symbol": "NSE:SBIN-EQ",
                "resolution": "D",
                "date_format": "1",
                "range_from": start.isoformat(),
                "range_to": end.isoformat(),
                "cont_flag": "1",
            }
        ),
    }
    results: dict[str, dict[str, Any]] = {}
    for name, call in calls.items():
        try:
            results[name] = _summary(call())
        except Exception as exc:
            results[name] = {
                "ok": False,
                "code": None,
                "message": f"{type(exc).__name__}: {exc}",
            }

    safe = {
        "status": "ok" if all(item["ok"] for item in results.values()) else "failed",
        "paper_only": True,
        "live_order_endpoints_enabled": False,
        "token_source": ".env",
        "env_last_modified": env_path.stat().st_mtime,
        "token_fingerprint": hashlib.sha256(token.encode("utf-8")).hexdigest()[:12],
        "token_app_binding_verified": _token_matches_app(token, client_id),
        "requests": results,
    }
    print(
        redact_text(
            json.dumps(safe, indent=2, sort_keys=True),
            secret_values=bundle.known_secret_values(),
        )
    )
    return 0 if safe["status"] == "ok" and safe["token_app_binding_verified"] is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
