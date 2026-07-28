from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stage1.validation.provenance import (  # noqa: E402
    ProvenanceError,
    validate_allowed_evidence_root,
)

ALLOWED_ROOTS = (
    "data/README.md",
    "data/bars",
    "data/decisions",
    "data/derived",
    "data/historical",
    "data/raw",
    "data/reports",
    "data/run_cards",
    "data/evaluation/development",
    "data/evaluation/validation",
    "state",
)
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_REPARSE_ATTRIBUTE = 0x400


def _is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
    )


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _hash_regular_file(path: Path) -> tuple[int, str]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ProvenanceError(f"INVENTORY_REJECTED: unreadable file {path}") from exc
    if _is_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise ProvenanceError(f"INVENTORY_REJECTED: unexpected file type {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before):
                raise ProvenanceError(f"INVENTORY_REJECTED: file changed {path}")
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after_open = os.fstat(stream.fileno())
    except ProvenanceError:
        raise
    except OSError as exc:
        raise ProvenanceError(f"INVENTORY_REJECTED: unreadable file {path}") from exc
    try:
        after_path = path.lstat()
    except OSError as exc:
        raise ProvenanceError(f"INVENTORY_REJECTED: disappeared file {path}") from exc
    if (
        _identity(before) != _identity(after_open)
        or _identity(before) != _identity(after_path)
        or _is_reparse(after_path)
        or not stat.S_ISREG(after_path.st_mode)
    ):
        raise ProvenanceError(f"INVENTORY_REJECTED: file changed {path}")
    return int(before.st_size), digest.hexdigest()


def _scan_directory(
    root: Path,
) -> tuple[
    tuple[int, int, int, int],
    tuple[tuple[str, str, tuple[int, int, int, int]], ...],
]:
    try:
        before = root.lstat()
    except OSError as exc:
        raise ProvenanceError(f"INVENTORY_REJECTED: unreadable directory {root}") from exc
    if _is_reparse(before) or not stat.S_ISDIR(before.st_mode):
        raise ProvenanceError(f"INVENTORY_REJECTED: unexpected directory {root}")
    try:
        with os.scandir(root) as entries:
            ordered = sorted(entries, key=lambda entry: os.fsencode(entry.name))
    except OSError as exc:
        raise ProvenanceError(f"INVENTORY_REJECTED: unreadable directory {root}") from exc
    captured: list[tuple[str, str, tuple[int, int, int, int]]] = []
    for entry in ordered:
        if entry.name.casefold() == "sealed":
            raise ProvenanceError("INVENTORY_REJECTED: sealed path")
        path = Path(entry.path)
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise ProvenanceError(f"INVENTORY_REJECTED: unreadable path {path}") from exc
        if _is_reparse(metadata):
            raise ProvenanceError(f"INVENTORY_REJECTED: reparse path {path}")
        if stat.S_ISDIR(metadata.st_mode):
            kind = "directory"
        elif stat.S_ISREG(metadata.st_mode):
            kind = "file"
        else:
            raise ProvenanceError(f"INVENTORY_REJECTED: unexpected file type {path}")
        captured.append((entry.name, kind, _identity(metadata)))
    try:
        after = root.lstat()
    except OSError as exc:
        raise ProvenanceError(f"INVENTORY_REJECTED: changed directory {root}") from exc
    if _identity(before) != _identity(after) or _is_reparse(after):
        raise ProvenanceError(f"INVENTORY_REJECTED: changed directory {root}")
    return _identity(before), tuple(captured)


_DirectorySnapshot = tuple[
    tuple[int, int, int, int],
    tuple[tuple[str, str, tuple[int, int, int, int]], ...],
]


def _walk_directory(
    root: Path,
    snapshots: dict[Path, _DirectorySnapshot],
) -> list[Path]:
    snapshots[root] = _scan_directory(root)
    result: list[Path] = []
    for name, kind, _ in snapshots[root][1]:
        path = root / name
        if kind == "directory":
            result.extend(_walk_directory(path, snapshots))
        else:
            result.append(path)
    if _scan_directory(root) != snapshots[root]:
        raise ProvenanceError(f"INVENTORY_REJECTED: changed directory {root}")
    return result


def _validate_tree_snapshots(
    snapshots: dict[Path, _DirectorySnapshot],
) -> None:
    # Re-scan every directory only after all files have been hashed. This
    # rejects late additions, removals, renames, substitutions, and partial
    # traversal instead of accepting a stale file list.
    for directory in sorted(
        snapshots,
        key=lambda path: (len(path.parts), os.fsencode(os.fspath(path))),
        reverse=True,
    ):
        if _scan_directory(directory) != snapshots[directory]:
            raise ProvenanceError(
                f"INVENTORY_REJECTED: tree changed during traversal {directory}"
            )


def _validate_file_snapshots(
    snapshots: dict[Path, tuple[int, int, int, int]],
) -> None:
    for path in sorted(snapshots, key=lambda item: os.fsencode(os.fspath(item))):
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ProvenanceError(
                f"INVENTORY_REJECTED: changed direct-file root {path}"
            ) from exc
        if (
            _is_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or _identity(metadata) != snapshots[path]
        ):
            raise ProvenanceError(
                f"INVENTORY_REJECTED: changed direct-file root {path}"
            )


def build_inventory(project_root: Path = ROOT) -> bytes:
    project = Path(os.path.abspath(project_root))
    files: list[Path] = []
    snapshots: dict[Path, _DirectorySnapshot] = {}
    direct_file_snapshots: dict[Path, tuple[int, int, int, int]] = {}
    for relative_root in ALLOWED_ROOTS:
        allowed = validate_allowed_evidence_root(project, Path(relative_root))
        metadata = allowed.lstat()
        if _is_reparse(metadata):
            raise ProvenanceError(f"INVENTORY_REJECTED: reparse allow-root {relative_root}")
        if stat.S_ISREG(metadata.st_mode):
            files.append(allowed)
            direct_file_snapshots[allowed] = _identity(metadata)
        elif stat.S_ISDIR(metadata.st_mode):
            files.extend(_walk_directory(allowed, snapshots))
        else:
            raise ProvenanceError(
                f"INVENTORY_REJECTED: unexpected allow-root type {relative_root}"
            )
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for path in files:
        try:
            relative = path.relative_to(project).as_posix()
        except ValueError as exc:
            raise ProvenanceError("INVENTORY_REJECTED: path escape") from exc
        if relative in seen or any(part.casefold() == "sealed" for part in Path(relative).parts):
            raise ProvenanceError("INVENTORY_REJECTED: duplicate or sealed path")
        seen.add(relative)
        size, sha256 = _hash_regular_file(path)
        records.append({"path": relative, "size": size, "sha256": sha256})
    _validate_tree_snapshots(snapshots)
    _validate_file_snapshots(direct_file_snapshots)
    records.sort(key=lambda record: str(record["path"]).casefold().encode("utf-8"))
    return b"".join(
        (
            json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\r\n"
        ).encode("utf-8")
        for record in records
    )


def _atomic_create(path: Path, content: bytes) -> None:
    project = Path(os.path.abspath(path.parents[2]))
    parent = path.parent
    if parent.exists():
        validate_allowed_evidence_root(project, parent)
    else:
        ancestor = parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        validate_allowed_evidence_root(project, ancestor)
        parent.mkdir(parents=True, exist_ok=False)
        validate_allowed_evidence_root(project, parent)
    if path.exists():
        raise ProvenanceError("INVENTORY_REJECTED: output already exists")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        metadata = temporary.lstat()
        if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise ProvenanceError("INVENTORY_REJECTED: unsafe temporary output")
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ProvenanceError("INVENTORY_REJECTED: output race") from exc
        except OSError as exc:
            raise ProvenanceError("INVENTORY_REJECTED: atomic install failed") from exc
    finally:
        temporary.unlink(missing_ok=True)


def write_inventory(label: str, payload: bytes, project_root: Path = ROOT) -> dict[str, object]:
    if _LABEL.fullmatch(label) is None:
        raise ProvenanceError("INVENTORY_REJECTED: invalid record label")
    output_root = Path(os.path.abspath(project_root)) / ".p0-inventory" / "remediation"
    output = output_root / f"{label}-evidence-hashes.jsonl"
    metadata_path = output_root / f"{label}-evidence-hashes.metadata.json"
    output_hash = hashlib.sha256(payload).hexdigest()
    record_count = payload.count(b"\n")
    metadata = {
        "allowed_roots": list(ALLOWED_ROOTS),
        "output_sha256": output_hash,
        "record_count": record_count,
        "schema_version": "allowed-evidence-inventory-metadata-v1",
    }
    metadata_payload = (
        json.dumps(
            metadata,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    output_exists = output.exists()
    metadata_exists = metadata_path.exists()
    if output_exists or metadata_exists:
        if not output_exists or not metadata_exists:
            raise ProvenanceError("INVENTORY_REJECTED: incomplete existing inventory")
        output_size, observed_output_hash = _hash_regular_file(output)
        metadata_size, observed_metadata_hash = _hash_regular_file(metadata_path)
        if (
            output_size != len(payload)
            or observed_output_hash != output_hash
            or metadata_size != len(metadata_payload)
            or observed_metadata_hash
            != hashlib.sha256(metadata_payload).hexdigest()
        ):
            raise ProvenanceError("INVENTORY_REJECTED: changed existing inventory")
        return metadata
    _atomic_create(output, payload)
    try:
        _atomic_create(metadata_path, metadata_payload)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the fixed P0-A evidence inventory")
    parser.add_argument("--label", required=True)
    arguments = parser.parse_args()
    try:
        metadata = write_inventory(arguments.label, build_inventory())
    except ProvenanceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(metadata, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
