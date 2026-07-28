from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stage1.config import load_config  # noqa: E402
from stage1.security import scan_git_candidate_files  # noqa: E402
from stage1.secrets import load_secrets, secret_presence  # noqa: E402


def _command(*parts: str) -> tuple[bool, str]:
    try:
        process = subprocess.run(
            parts,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, ""
    output = (process.stdout or process.stderr).strip()
    return process.returncode == 0, output


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ollama_models(output: str) -> dict[str, str]:
    models: dict[str, str] = {}
    for line in output.splitlines()[1:]:
        columns = line.split()
        if len(columns) >= 2:
            models[columns[0]] = columns[1]
    return models


def _full_ollama_digest(model_name: str) -> tuple[str | None, str | None]:
    if ":" in model_name:
        repository, tag = model_name.rsplit(":", 1)
    else:
        repository, tag = model_name, "latest"
    manifest_path = (
        Path.home()
        / ".ollama"
        / "models"
        / "manifests"
        / "registry.ollama.ai"
        / "library"
        / repository
        / tag
    )
    if not manifest_path.exists():
        return None, None
    manifest_bytes = manifest_path.read_bytes()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    try:
        manifest = json.loads(manifest_bytes)
        config_digest = str(manifest.get("config", {}).get("digest") or "")
    except (json.JSONDecodeError, AttributeError):
        config_digest = ""
    return config_digest or None, manifest_hash


def _ollama_model_details(model_name: str) -> dict[str, str | None]:
    ok, output = _command("ollama", "show", "--verbose", model_name)
    if not ok:
        return {
            "parameters": None,
            "context_length": None,
            "embedding_length": None,
            "quantization": None,
        }

    def value(label: str) -> str | None:
        match = re.search(
            rf"(?im)^\s*{re.escape(label)}\s+(.+?)\s*$",
            output,
        )
        return match.group(1).strip() if match else None

    return {
        "parameters": value("parameters"),
        "context_length": value("context length"),
        "embedding_length": value("embedding length"),
        "quantization": value("quantization"),
    }


def _git_identity() -> tuple[str, bool]:
    ok, commit = _command("git", "rev-parse", "HEAD")
    status_ok, status = _command("git", "status", "--porcelain")
    return (commit if ok else "UNCOMMITTED"), (bool(status) if status_ok else True)


def _nvidia_identity() -> dict[str, Any]:
    ok, csv_output = _command(
        "nvidia-smi",
        "--query-gpu=name,memory.total,driver_version",
        "--format=csv,noheader,nounits",
    )
    if not ok or not csv_output:
        return {"available": False}
    first = csv_output.splitlines()[0]
    parts = [part.strip() for part in first.split(",")]
    full_ok, full_output = _command("nvidia-smi")
    cuda_match = re.search(r"CUDA Version:\s*([0-9.]+)", full_output) if full_ok else None
    return {
        "available": True,
        "name": parts[0] if parts else "unknown",
        "memory_mib": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None,
        "driver_version": parts[2] if len(parts) > 2 else None,
        "cuda_reported_by_driver": cuda_match.group(1) if cuda_match else None,
    }


def _installed_packages() -> dict[str, str]:
    packages: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            packages[str(name).lower()] = distribution.version
    return dict(sorted(packages.items()))


def _portable_executable_path() -> str:
    executable = Path(sys.executable).resolve()
    try:
        return executable.relative_to(ROOT).as_posix()
    except ValueError:
        return executable.name


def build_manifest(config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    git_commit, dirty = _git_identity()
    ollama_ok, ollama_version = _command("ollama", "--version")
    list_ok, list_output = _command("ollama", "list")
    models = _ollama_models(list_output) if list_ok else {}
    config_digest, ollama_manifest_hash = _full_ollama_digest(
        config.models.local.name
    )
    model_details = _ollama_model_details(config.models.local.name)
    manifest: dict[str, Any] = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "project": "stage1-paper-agent",
        "mode": config.mode,
        "live_order_endpoints_enabled": config.live_order_endpoints_enabled,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": _portable_executable_path(),
            "executable_sha256": _sha256_file(Path(sys.executable)),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "git": {"commit": git_commit, "dirty_worktree": dirty},
        "dependency_lock_sha256": _sha256_file(ROOT / "requirements.lock"),
        "config_sha256": _sha256_file(config_path),
        "dependencies": _installed_packages(),
        "ollama": {
            "available": ollama_ok,
            "version_output": ollama_version if ollama_ok else None,
            "model_name": config.models.local.name,
            "list_id": models.get(config.models.local.name),
            "config_digest": config_digest,
            "manifest_sha256": ollama_manifest_hash,
            **model_details,
        },
        "nvidia": _nvidia_identity(),
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    manifest["runtime_manifest_sha256"] = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    return manifest


def _result(label: str, status: str, detail: str) -> dict[str, str]:
    print(f"{status:4}  {label:<26} {detail}")
    return {"label": label, "status": status, "detail": detail}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Stage 1 offline readiness.")
    parser.add_argument("--require-model", action="store_true")
    parser.add_argument("--require-fyers", action="store_true")
    parser.add_argument("--require-telegram", action="store_true")
    parser.add_argument("--write-manifest", action="store_true")
    arguments = parser.parse_args()

    results: list[dict[str, str]] = []
    config_path = ROOT / "config" / "stage1.yaml"

    python_ok = sys.version_info[:2] == (3, 12)
    results.append(
        _result(
            "Python 3.12",
            "PASS" if python_ok else "FAIL",
            platform.python_version(),
        )
    )
    in_venv = sys.prefix != sys.base_prefix
    results.append(
        _result(
            "Virtual environment",
            "PASS" if in_venv else "FAIL",
            ".venv" if in_venv else "not active",
        )
    )
    git_ok, git_version = _command("git", "--version")
    results.append(_result("Git", "PASS" if git_ok else "FAIL", git_version or "missing"))
    results.append(
        _result(
            "Git repository",
            "PASS" if (ROOT / ".git").exists() else "FAIL",
            "initialized" if (ROOT / ".git").exists() else "missing",
        )
    )
    lock_exists = (ROOT / "requirements.lock").exists()
    results.append(
        _result(
            "Hash-locked dependencies",
            "PASS" if lock_exists else "FAIL",
            "requirements.lock" if lock_exists else "missing",
        )
    )
    try:
        secret_findings = scan_git_candidate_files(ROOT)
    except Exception as exc:
        results.append(_result("Credential scan", "FAIL", type(exc).__name__))
    else:
        results.append(
            _result(
                "Credential scan",
                "PASS" if not secret_findings else "FAIL",
                (
                    "no known credential patterns"
                    if not secret_findings
                    else f"{len(secret_findings)} file/rule matches"
                ),
            )
        )

    try:
        config = load_config(config_path)
    except Exception as exc:
        config = None
        results.append(_result("Stage 1 configuration", "FAIL", type(exc).__name__))
    else:
        results.append(
            _result(
                "Stage 1 configuration",
                "PASS",
                "paper / live endpoints false / 12 symbols",
            )
        )

    disk = shutil.disk_usage(ROOT)
    free_gb = disk.free / (1024**3)
    results.append(
        _result(
            "Free disk",
            "PASS" if free_gb >= 25 else "FAIL",
            f"{free_gb:.2f} GB",
        )
    )

    ollama_ok, ollama_version = _command("ollama", "--version")
    results.append(
        _result("Ollama", "PASS" if ollama_ok else "FAIL", ollama_version or "missing")
    )
    list_ok, list_output = _command("ollama", "list")
    models = _ollama_models(list_output) if list_ok else {}
    model_name = config.models.local.name if config else "qwen3.5:9b"
    model_present = model_name in models
    model_status = "PASS" if model_present else ("FAIL" if arguments.require_model else "WAIT")
    results.append(
        _result(
            "Local model",
            model_status,
            models.get(model_name, f"{model_name} not installed"),
        )
    )
    configured_digest = config.models.local.digest if config else ""
    installed_digest, _ = _full_ollama_digest(model_name)
    identity_matches = (
        model_present
        and installed_digest is not None
        and configured_digest == installed_digest
    )
    identity_status = (
        "PASS"
        if identity_matches
        else (
            "FAIL"
            if model_present or arguments.require_model
            else "WAIT"
        )
    )
    results.append(
        _result(
            "Local model identity",
            identity_status,
            (
                "full digest matches frozen config"
                if identity_matches
                else "install model and record its full digest"
            ),
        )
    )
    model_details = _ollama_model_details(model_name) if model_present else {}
    installed_quantization = model_details.get("quantization")
    quantization_matches = (
        model_present
        and installed_quantization == (
            config.models.local.quantization if config else "Q4_K_M"
        )
    )
    quantization_status = (
        "PASS"
        if quantization_matches
        else (
            "FAIL"
            if model_present or arguments.require_model
            else "WAIT"
        )
    )
    results.append(
        _result(
            "Local quantization",
            quantization_status,
            (
                str(installed_quantization)
                if model_present
                else "verify Q4_K_M after installation"
            ),
        )
    )

    nvidia = _nvidia_identity()
    gpu_ok = bool(nvidia.get("available")) and int(nvidia.get("memory_mib") or 0) >= 12_000
    results.append(
        _result(
            "NVIDIA GPU",
            "PASS" if gpu_ok else "FAIL",
            (
                f"{nvidia.get('name')} / {nvidia.get('memory_mib')} MiB / "
                f"driver {nvidia.get('driver_version')}"
                if nvidia.get("available")
                else "nvidia-smi unavailable"
            ),
        )
    )

    try:
        bundle = load_secrets(ROOT / ".env")
        presence = secret_presence(bundle)
    except Exception as exc:
        presence = {}
        results.append(_result("Secret boundary", "FAIL", type(exc).__name__))
    else:
        results.append(_result("Secret boundary", "PASS", "paper-only validation active"))
        fyers_ready = all(
            presence.get(name, False)
            for name in ("fyers_client_id", "fyers_secret", "fyers_access_token")
        )
        fyers_status = "PASS" if fyers_ready else (
            "FAIL" if arguments.require_fyers else "WAIT"
        )
        results.append(
            _result(
                "FYERS configuration",
                fyers_status,
                "configured" if fyers_ready else "manual authorization required",
            )
        )
        telegram_ready = all(
            presence.get(name, False)
            for name in ("telegram_bot_token", "telegram_chat_id")
        )
        telegram_status = "PASS" if telegram_ready else (
            "FAIL" if arguments.require_telegram else "WAIT"
        )
        results.append(
            _result(
                "Telegram configuration",
                telegram_status,
                "configured" if telegram_ready else "manual bot setup deferred",
            )
        )

    if arguments.write_manifest and config is not None:
        manifest = build_manifest(config_path)
        target = ROOT / "state" / "runtime_manifest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
        print(f"PASS  {'Runtime manifest':<26} state/runtime_manifest.json")

    failed = [result for result in results if result["status"] == "FAIL"]
    waiting = [result for result in results if result["status"] == "WAIT"]
    print(
        f"\nOffline readiness: {'FAIL' if failed else 'PASS'} "
        f"({len(failed)} failed, {len(waiting)} manual/deferred)"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
