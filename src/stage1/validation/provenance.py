from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

SCHEMA_VERSION = "baseline-manifest-v1"
LEGACY_STATUS = "LEGACY_UNATTESTED"
DEFAULT_MANIFEST_NAME = "baseline-manifest-v1.json"

_PRIVATE_KEY_PATTERN = re.compile(
    rb"-----BEGIN (?:(?:ENCRYPTED|RSA|EC|DSA|OPENSSH) )?PRIVATE KEY-----"
)
_SECRET_PATTERNS = (
    re.compile(rb"\b[0-9]{8,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(rb"\bAIza[A-Za-z0-9_-]{30,}\b"),
    re.compile(rb"\bgh[psoru]_[A-Za-z0-9]{30,}\b"),
    re.compile(
        rb"\b(?:[A-Z0-9]{8,16}-100:)?"
        rb"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"
    ),
)
_PROHIBITED_ROOTS = {
    ".p0-inventory",
    "artifacts/provenance",
    "build",
    "credentials",
    "data/bars",
    "data/decisions",
    "data/derived",
    "data/evaluation",
    "data/historical",
    "data/raw",
    "data/reports",
    "data/run_cards",
    "dist",
    "logs",
    "node_modules",
    "state",
}
_PROHIBITED_PARTS = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".wrangler",
    "__pycache__",
    "dist",
    "node_modules",
}
_ALLOWED_BUILD_TOOL_PATHS = {
    "dashboard/build/sites-vite-plugin.ts",
}
_PRIVATE_SUFFIXES = (".key", ".p12", ".pfx")
_MANIFEST_FIELDS = {
    "approved_by",
    "build_tool_versions",
    "code_commit",
    "created_at",
    "dependency_lock_hash",
    "exclusion_policy_hash",
    "legacy_evidence_status",
    "manifest_hash",
    "manifest_id",
    "platform",
    "python_runtime",
    "schema_version",
    "sealed_test_exclusion_attested",
    "secret_scan_result",
    "tracked_path_count",
    "tree_hash",
}
_REPARSE_ATTRIBUTE = 0x400


class ProvenanceError(RuntimeError):
    """Fail-closed baseline provenance error."""


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise ProvenanceError(f"PROVENANCE_REJECTED: unreadable path {path}") from exc


def _is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
    )


def _assert_no_reparse_components(root: Path, path: Path) -> None:
    root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(path))
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ProvenanceError("PROVENANCE_REJECTED: path escape") from exc
    cursor = root
    root_metadata = _lstat(cursor)
    if _is_reparse(root_metadata) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ProvenanceError("PROVENANCE_REJECTED: unsafe repository root")
    for component in relative.parts:
        cursor = cursor / component
        metadata = _lstat(cursor)
        if _is_reparse(metadata):
            raise ProvenanceError(
                f"PROVENANCE_REJECTED: reparse component {component}"
            )


def _validate_repo_relative(value: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise ProvenanceError("PROVENANCE_REJECTED: invalid relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value.startswith("/")
        or ":" in path.parts[0]
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ProvenanceError("PROVENANCE_REJECTED: invalid relative path")
    return path


def validate_allowed_evidence_root(
    project_root: Path,
    candidate: Path,
    *,
    is_reparse_point: Any | None = None,
) -> Path:
    """Validate an explicit evidence allow-root before recursive traversal."""
    project = Path(os.path.abspath(project_root))
    project_metadata = _lstat(project)
    if _is_reparse(project_metadata) or not stat.S_ISDIR(project_metadata.st_mode):
        raise ProvenanceError("PROVENANCE_REJECTED: unsafe project root")
    lexical = candidate if candidate.is_absolute() else project / candidate
    lexical = Path(os.path.abspath(lexical))
    try:
        relative = lexical.relative_to(project)
    except ValueError as exc:
        raise ProvenanceError("PROVENANCE_REJECTED: evidence root outside project") from exc
    if any(part.casefold() == "sealed" for part in relative.parts):
        raise ProvenanceError("PROVENANCE_REJECTED: lexical sealed path")
    detector = is_reparse_point
    cursor = project
    for component in relative.parts:
        cursor = cursor / component
        try:
            metadata = cursor.lstat()
            reparse = (
                bool(detector(cursor))
                if detector is not None
                else _is_reparse(metadata)
            )
        except OSError as exc:
            raise ProvenanceError(
                "PROVENANCE_REJECTED: ambiguous evidence root"
            ) from exc
        if reparse:
            raise ProvenanceError("PROVENANCE_REJECTED: reparse evidence root")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise ProvenanceError("PROVENANCE_REJECTED: ambiguous evidence root") from exc
    try:
        resolved_relative = resolved.relative_to(project.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ProvenanceError("PROVENANCE_REJECTED: resolved root outside project") from exc
    if any(part.casefold() == "sealed" for part in resolved_relative.parts):
        raise ProvenanceError("PROVENANCE_REJECTED: resolved sealed path")
    return resolved


def _git(root: Path, *args: str, text: bool = False) -> bytes | str:
    process = subprocess.run(
        ("git", *args),
        cwd=root,
        check=False,
        capture_output=True,
        text=text,
    )
    if process.returncode != 0:
        error = process.stderr if text else process.stderr.decode("utf-8", "replace")
        raise ProvenanceError(f"Git command failed: {error.strip()}")
    return process.stdout


def _clean_tree(root: Path) -> None:
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all", text=True)
    if str(status).strip():
        raise ProvenanceError("PROVENANCE_REJECTED: Git worktree is not clean")


def _tracked_paths(root: Path, commit: str) -> list[str]:
    output = _git(root, "ls-tree", "-r", "--name-only", "-z", commit)
    assert isinstance(output, bytes)
    try:
        paths = [
            item.decode("utf-8", "strict").replace("\\", "/")
            for item in output.split(b"\0")
            if item
        ]
    except UnicodeDecodeError as exc:
        raise ProvenanceError("PROVENANCE_REJECTED: non-UTF-8 Git path") from exc
    paths.sort(key=lambda item: item.encode("utf-8"))
    return paths


def _reject_path(relative_path: str) -> None:
    path = _validate_repo_relative(relative_path)
    parts = [part.casefold() for part in path.parts]
    name = parts[-1]
    if set(parts) & _PROHIBITED_PARTS:
        raise ProvenanceError(f"PROVENANCE_REJECTED: prohibited path {relative_path}")
    if "build" in parts and relative_path not in _ALLOWED_BUILD_TOOL_PATHS:
        raise ProvenanceError(f"PROVENANCE_REJECTED: prohibited path {relative_path}")
    for prohibited in _PROHIBITED_ROOTS:
        prohibited_parts = prohibited.split("/")
        if parts[: len(prohibited_parts)] == prohibited_parts:
            if relative_path != "state/.gitkeep":
                raise ProvenanceError(
                    f"PROVENANCE_REJECTED: runtime path {relative_path}"
                )
    if "sealed" in parts:
        raise ProvenanceError(f"PROVENANCE_REJECTED: sealed path {relative_path}")
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        raise ProvenanceError(f"PROVENANCE_REJECTED: environment file {relative_path}")
    if name.endswith(_PRIVATE_SUFFIXES) or "private" in name:
        raise ProvenanceError(f"PROVENANCE_REJECTED: private key path {relative_path}")
    if name.endswith(".log"):
        raise ProvenanceError(f"PROVENANCE_REJECTED: log path {relative_path}")


def _blob(root: Path, commit: str, relative_path: str) -> bytes:
    _validate_repo_relative(relative_path)
    output = _git(root, "show", f"{commit}:{relative_path}")
    assert isinstance(output, bytes)
    return output


def _combined_lock_hash(root: Path, commit: str) -> str:
    digest = hashlib.sha256()
    for relative_path in ("dashboard/package-lock.json", "requirements.lock"):
        try:
            content = _blob(root, commit, relative_path)
        except ProvenanceError as exc:
            raise ProvenanceError(
                f"PROVENANCE_REJECTED: required dependency lock missing: {relative_path}"
            ) from exc
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def _scan_committed_blobs(root: Path, commit: str, paths: Iterable[str]) -> None:
    for relative_path in paths:
        content = _blob(root, commit, relative_path)
        if _PRIVATE_KEY_PATTERN.search(content):
            raise ProvenanceError(
                f"PROVENANCE_REJECTED: private key content in {relative_path}"
            )
        if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
            raise ProvenanceError(
                f"PROVENANCE_REJECTED: secret-like content in {relative_path}"
            )


def _version(command: tuple[str, ...]) -> str:
    try:
        process = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError:
        return "unavailable"
    first_line = (process.stdout or process.stderr).splitlines()
    return first_line[0].strip() if first_line else "unavailable"


def _manifest_hash(payload: dict[str, Any]) -> str:
    material = {key: value for key, value in payload.items() if key != "manifest_hash"}
    return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


def _validate_manifest_schema(manifest: dict[str, Any]) -> None:
    if set(manifest) != _MANIFEST_FIELDS:
        missing = sorted(_MANIFEST_FIELDS - set(manifest))
        unknown = sorted(set(manifest) - _MANIFEST_FIELDS)
        raise ProvenanceError(
            f"PROVENANCE_REJECTED: manifest schema missing={missing} unknown={unknown}"
        )
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ProvenanceError("PROVENANCE_REJECTED: manifest schema version")
    if manifest["sealed_test_exclusion_attested"] is not True:
        raise ProvenanceError("PROVENANCE_REJECTED: sealed attestation required")
    if manifest["secret_scan_result"] != "PASS":
        raise ProvenanceError("PROVENANCE_REJECTED: secret scan not PASS")
    if not isinstance(manifest["tracked_path_count"], int):
        raise ProvenanceError("PROVENANCE_REJECTED: invalid tracked path count")


def build_baseline_manifest(
    root: Path,
    *,
    approved_by: str,
    sealed_test_exclusion_attested: bool,
    commit: str = "HEAD",
) -> dict[str, Any]:
    root = Path(os.path.abspath(root))
    _assert_no_reparse_components(root, root)
    _clean_tree(root)
    if sealed_test_exclusion_attested is not True:
        raise ProvenanceError("PROVENANCE_REJECTED: sealed attestation required")
    if not approved_by or approved_by.strip() != approved_by:
        raise ProvenanceError("PROVENANCE_REJECTED: invalid approver")
    code_commit = str(
        _git(root, "rev-parse", "--verify", f"{commit}^{{commit}}", text=True)
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", code_commit):
        raise ProvenanceError("PROVENANCE_REJECTED: invalid commit hash")
    tree_hash = str(_git(root, "rev-parse", f"{code_commit}^{{tree}}", text=True)).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", tree_hash):
        raise ProvenanceError("PROVENANCE_REJECTED: invalid tree hash")
    paths = _tracked_paths(root, code_commit)
    if not paths:
        raise ProvenanceError("PROVENANCE_REJECTED: commit has no tracked paths")
    for relative_path in paths:
        _reject_path(relative_path)
    _scan_committed_blobs(root, code_commit, paths)
    lock_hash = _combined_lock_hash(root, code_commit)
    exclusion_policy_hash = hashlib.sha256(
        _blob(root, code_commit, ".gitignore")
    ).hexdigest()
    commit_epoch = int(
        str(_git(root, "show", "-s", "--format=%ct", code_commit, text=True)).strip()
    )
    created_at = datetime.fromtimestamp(commit_epoch, timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "approved_by": approved_by,
        "build_tool_versions": {
            "git": _version(("git", "--version")),
            "node": _version(("node", "--version")),
            "python": platform.python_version(),
        },
        "code_commit": code_commit,
        "created_at": created_at,
        "dependency_lock_hash": lock_hash,
        "exclusion_policy_hash": exclusion_policy_hash,
        "legacy_evidence_status": LEGACY_STATUS,
        "manifest_id": f"baseline-{code_commit[:12]}",
        "platform": f"{platform.system()}-{platform.machine()}",
        "python_runtime": (
            f"{platform.python_implementation()} {platform.python_version()}"
        ),
        "schema_version": SCHEMA_VERSION,
        "sealed_test_exclusion_attested": True,
        "secret_scan_result": "PASS",
        "tracked_path_count": len(paths),
        "tree_hash": tree_hash,
    }
    payload["manifest_hash"] = _manifest_hash(payload)
    _validate_manifest_schema(payload)
    return payload


def _atomic_create(path: Path, content: bytes) -> None:
    if path.exists():
        raise ProvenanceError("PROVENANCE_REJECTED: output already exists")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        metadata = _lstat(temporary)
        if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise ProvenanceError("PROVENANCE_REJECTED: unsafe temporary output")
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ProvenanceError("PROVENANCE_REJECTED: output race") from exc
        except OSError as exc:
            raise ProvenanceError("PROVENANCE_REJECTED: atomic install failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_manifest(root: Path, manifest: dict[str, Any]) -> Path:
    root = Path(os.path.abspath(root))
    output_root = root / "artifacts" / "provenance"
    if output_root.exists():
        _assert_no_reparse_components(root, output_root)
    else:
        parent = output_root.parent
        if parent.exists():
            _assert_no_reparse_components(root, parent)
        else:
            _assert_no_reparse_components(root, root)
            parent.mkdir(mode=0o700)
            _assert_no_reparse_components(root, parent)
        output_root.mkdir(mode=0o700)
    output = output_root / DEFAULT_MANIFEST_NAME
    _validate_manifest_schema(manifest)
    if _manifest_hash(manifest) != manifest.get("manifest_hash"):
        raise ProvenanceError("PROVENANCE_REJECTED: manifest hash mismatch")
    _atomic_create(output, canonical_json_bytes(manifest))
    return output
