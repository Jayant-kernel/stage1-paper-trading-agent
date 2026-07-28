from __future__ import annotations

import argparse
import getpass
import os
import shutil
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stage1.adapters.fyers_auth import (  # noqa: E402
    create_authorization_url,
    exchange_callback_for_token,
    store_access_token,
)
from stage1.secrets import load_secrets  # noqa: E402


def _ensure_local_env(env_path: Path) -> None:
    if not env_path.exists():
        shutil.copyfile(ROOT / ".env.example", env_path)
        print("Created local .env from .env.example.")


def start(*, open_browser: bool) -> int:
    env_path = ROOT / ".env"
    _ensure_local_env(env_path)
    bundle = load_secrets(env_path)
    state_path = ROOT / "state" / "fyers_oauth_state.json"
    url = create_authorization_url(bundle, state_path=state_path)
    print("FYERS authorization URL (complete login and 2FA manually):")
    print(url)
    if open_browser:
        webbrowser.open(url, new=1)
    print("After FYERS redirects, run this script with the 'finish' command.")
    return 0


def finish(*, callback_environment: str | None = None) -> int:
    env_path = ROOT / ".env"
    _ensure_local_env(env_path)
    bundle = load_secrets(env_path)
    if callback_environment is None:
        callback_url = getpass.getpass(
            "Paste the complete FYERS callback URL (input is hidden): "
        )
    else:
        callback_url = os.environ.pop(callback_environment, "")
        if not callback_url.strip():
            raise RuntimeError("the local callback handoff was empty")
    token = exchange_callback_for_token(
        bundle,
        callback_url=callback_url,
        state_path=ROOT / "state" / "fyers_oauth_state.json",
    )
    store_access_token(env_path, token)
    print("FYERS access token stored in the untracked local .env file.")
    print("The token was not printed or logged.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manual FYERS API v3 authorization helper (no password/2FA automation)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start", help="generate the FYERS login URL")
    start_parser.add_argument(
        "--open-browser",
        action="store_true",
        help="open the URL in the default browser",
    )
    finish_parser = subparsers.add_parser(
        "finish", help="exchange the manually obtained auth code"
    )
    finish_parser.add_argument(
        "--callback-environment",
        help=argparse.SUPPRESS,
    )
    arguments = parser.parse_args()
    if arguments.command == "start":
        return start(open_browser=arguments.open_browser)
    return finish(callback_environment=arguments.callback_environment)


if __name__ == "__main__":
    raise SystemExit(main())
