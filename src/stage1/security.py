from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SecretFinding:
    relative_path: str
    rule: str


_RULES = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "telegram_bot_token": re.compile(r"\b[0-9]{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "google_api_key": re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),
    "github_token": re.compile(r"\bgh[psoru]_[A-Za-z0-9]{30,}\b"),
    "jwt_or_fyers_token": re.compile(
        r"\b(?:[A-Z0-9]{8,16}-100:)?"
        r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"
    ),
}


def candidate_git_files(root: Path) -> list[Path]:
    process = subprocess.run(
        (
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ),
        cwd=root,
        check=True,
        capture_output=True,
    )
    relative_names = [
        name.decode("utf-8", errors="surrogateescape")
        for name in process.stdout.split(b"\0")
        if name
    ]
    return [root / name for name in relative_names]


def scan_git_candidate_files(root: Path) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for path in candidate_git_files(root):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative_path = path.relative_to(root).as_posix()
        for rule, pattern in _RULES.items():
            if pattern.search(text):
                findings.append(SecretFinding(relative_path=relative_path, rule=rule))
    return findings

