from __future__ import annotations

import json
import secrets as secure_random
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from stage1.secrets import SecretBundle, store_local_env_values


def create_authorization_url(
    bundle: SecretBundle,
    *,
    state_path: Path,
) -> str:
    client_id, secret_key = bundle.require_fyers_app()
    from fyers_apiv3 import fyersModel

    oauth_state = secure_random.token_urlsafe(32)
    session = fyersModel.SessionModel(
        client_id=client_id,
        redirect_uri=bundle.fyers_redirect_uri,
        response_type="code",
        state=oauth_state,
        secret_key=secret_key,
        grant_type="authorization_code",
    )
    url = session.generate_authcode()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_payload = {
        "state": oauth_state,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "redirect_uri": bundle.fyers_redirect_uri,
    }
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state_payload, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(state_path)
    return str(url)


def exchange_callback_for_token(
    bundle: SecretBundle,
    *,
    callback_url: str,
    state_path: Path,
) -> str:
    client_id, secret_key = bundle.require_fyers_app()
    if not state_path.exists():
        raise RuntimeError("no pending FYERS authorization state; run the start command first")
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))

    parsed = urlparse(callback_url.strip())
    expected_redirect = urlparse(bundle.fyers_redirect_uri)
    if (
        parsed.scheme,
        parsed.hostname,
        parsed.port,
        parsed.path.rstrip("/"),
    ) != (
        expected_redirect.scheme,
        expected_redirect.hostname,
        expected_redirect.port,
        expected_redirect.path.rstrip("/"),
    ):
        raise RuntimeError("callback URL does not match the configured FYERS redirect URI")
    query = parse_qs(parsed.query)
    authorization_code = _single_query_value(query, ("auth_code", "code"))
    returned_state = _single_query_value(query, ("state",))
    assert authorization_code is not None
    assert returned_state is not None
    expected_state = state_payload.get("state")
    if not secure_random.compare_digest(
        returned_state,
        str(expected_state),
    ):
        raise RuntimeError("FYERS callback state did not match the pending login")

    from fyers_apiv3 import fyersModel

    session = fyersModel.SessionModel(
        client_id=client_id,
        redirect_uri=bundle.fyers_redirect_uri,
        response_type="code",
        state=str(expected_state),
        secret_key=secret_key,
        grant_type="authorization_code",
    )
    session.set_token(authorization_code)
    response = session.generate_token()
    if not isinstance(response, dict) or not response.get("access_token"):
        raise RuntimeError("FYERS rejected the authorization code; no token was stored")
    state_path.unlink(missing_ok=True)
    return str(response["access_token"])


def store_access_token(env_path: Path, token: str) -> None:
    store_local_env_values(env_path, {"FYERS_ACCESS_TOKEN": token})


def _single_query_value(
    query: dict[str, list[str]],
    names: tuple[str, ...],
    *,
    required: bool = True,
) -> str | None:
    for name in names:
        values = query.get(name)
        if values and values[0]:
            return values[0]
    if required:
        raise RuntimeError(f"callback URL did not contain {' or '.join(names)}")
    return None
