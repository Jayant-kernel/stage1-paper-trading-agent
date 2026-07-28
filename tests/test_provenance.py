from __future__ import annotations

import json
import os
import shutil
import subprocess
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

from stage1.validation.provenance import (
    LEGACY_STATUS,
    ProvenanceError,
    build_baseline_manifest,
    canonical_json_bytes,
    validate_allowed_evidence_root,
    write_manifest,
)


def _run(root: Path, *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=root, check=True, capture_output=True, text=True)


def _repository(tmp_path: Path, *, missing_lock: bool = False) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "git", "init", "-q")
    _run(root, "git", "config", "user.name", "P0 Test")
    _run(root, "git", "config", "user.email", "p0-test@example.invalid")
    (root / ".gitignore").write_text(
        ".env\nstate/*\n!state/.gitkeep\nartifacts/provenance/\n",
        encoding="utf-8",
    )
    (root / "dashboard").mkdir()
    (root / "dashboard" / "package-lock.json").write_text("{}\n", encoding="utf-8")
    if not missing_lock:
        (root / "requirements.lock").write_text("pytest==9.0.2\n", encoding="utf-8")
    (root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    _run(root, "git", "add", ".")
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_DATE": "2026-07-28T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-07-28T00:00:00Z",
        }
    )
    subprocess.run(
        ("git", "commit", "-q", "-m", "fixture"),
        cwd=root,
        check=True,
        env=env,
    )
    return root


def test_canonical_json_is_sorted_utf8_lf() -> None:
    assert canonical_json_bytes({"z": 1, "a": "₹"}) == (
        '{"a":"₹","z":1}\n'.encode("utf-8")
    )


def test_same_clean_commit_reproduces_identical_manifest(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    first = build_baseline_manifest(
        root,
        approved_by="Jayant Saxena",
        sealed_test_exclusion_attested=True,
    )
    second = build_baseline_manifest(
        root,
        approved_by="Jayant Saxena",
        sealed_test_exclusion_attested=True,
    )
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert first["legacy_evidence_status"] == LEGACY_STATUS
    assert first["secret_scan_result"] == "PASS"


def test_generated_manifest_is_ignored_and_does_not_dirty_baseline(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    manifest = build_baseline_manifest(
        root,
        approved_by="Jayant Saxena",
        sealed_test_exclusion_attested=True,
    )
    output = write_manifest(root, manifest)
    assert output.name == "baseline-manifest-v1.json"
    assert output.read_bytes() == canonical_json_bytes(manifest)
    assert _run(root, "git", "status", "--porcelain").stdout == ""


def test_dirty_tracked_and_untracked_candidate_files_fail_closed(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    (root / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(ProvenanceError, match="not clean"):
        build_baseline_manifest(
            root,
            approved_by="Jayant Saxena",
            sealed_test_exclusion_attested=True,
        )
    _run(root, "git", "restore", "source.py")
    (root / "untracked.txt").write_text("candidate\n", encoding="utf-8")
    with pytest.raises(ProvenanceError, match="not clean"):
        build_baseline_manifest(
            root,
            approved_by="Jayant Saxena",
            sealed_test_exclusion_attested=True,
        )


def test_missing_dependency_lock_fails_closed(tmp_path: Path) -> None:
    root = _repository(tmp_path, missing_lock=True)
    with pytest.raises(ProvenanceError, match="required dependency lock missing"):
        build_baseline_manifest(
            root,
            approved_by="Jayant Saxena",
            sealed_test_exclusion_attested=True,
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        ".env.production",
        "credentials/token.txt",
        "data/raw/tick.json",
        "data/evaluation/sealed/canary.txt",
        "logs/runtime.log",
        "security/research.private.key",
        "state/live.json",
        "dashboard/dist/bundle.js",
        "packages/build/output.js",
        "dashboard/node_modules/package/index.js",
        "dashboard/.wrangler/cache/blob",
        "nested/.pytest_cache/state",
    ],
)
def test_prohibited_committed_paths_fail_closed(
    tmp_path: Path,
    relative_path: str,
) -> None:
    root = _repository(tmp_path)
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-a-secret\n", encoding="utf-8")
    _run(root, "git", "add", "-f", relative_path)
    _run(root, "git", "commit", "-q", "-m", "prohibited fixture")
    with pytest.raises(ProvenanceError, match="PROVENANCE_REJECTED"):
        build_baseline_manifest(
            root,
            approved_by="Jayant Saxena",
            sealed_test_exclusion_attested=True,
        )


def test_exact_approved_build_tool_source_is_allowed(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    tool = root / "dashboard" / "build" / "sites-vite-plugin.ts"
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text("export const fixture = true;\n", encoding="utf-8")
    _run(root, "git", "add", tool.relative_to(root).as_posix())
    _run(root, "git", "commit", "-q", "-m", "approved build tool fixture")
    manifest = build_baseline_manifest(
        root,
        approved_by="Jayant Saxena",
        sealed_test_exclusion_attested=True,
    )
    assert manifest["tracked_path_count"] >= 1


def test_secret_like_committed_blob_fails(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    secret_fixture = "123456789:" + ("a" * 35)
    (root / "source.py").write_text(
        f'TOKEN = "{secret_fixture}"\n',
        encoding="utf-8",
    )
    _run(root, "git", "add", "source.py")
    _run(root, "git", "commit", "-q", "-m", "secret fixture")
    with pytest.raises(ProvenanceError, match="secret-like"):
        build_baseline_manifest(
            root,
            approved_by="Jayant Saxena",
            sealed_test_exclusion_attested=True,
        )


@pytest.mark.parametrize(
    "header",
    (
        "PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "DSA PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
    ),
)
def test_every_private_key_pem_header_fails(
    tmp_path: Path,
    header: str,
) -> None:
    root = _repository(tmp_path)
    (root / "source.py").write_text(
        f"-----BEGIN {header}-----\nfixture\n-----END {header}-----\n",
        encoding="utf-8",
    )
    _run(root, "git", "add", "source.py")
    _run(root, "git", "commit", "-q", "-m", "private fixture")
    with pytest.raises(ProvenanceError, match="private key content"):
        build_baseline_manifest(
            root,
            approved_by="Jayant Saxena",
            sealed_test_exclusion_attested=True,
        )


def test_inventory_allow_root_rejects_lexical_sealed_before_detector(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    canary = project / "data" / "evaluation" / "sealed" / "canary.txt"
    canary.parent.mkdir(parents=True)
    canary.write_text("must-not-open", encoding="utf-8")
    called = False

    def detector(_: Path) -> bool:
        nonlocal called
        called = True
        return False

    with pytest.raises(ProvenanceError, match="lexical sealed"):
        validate_allowed_evidence_root(
            project,
            canary.parent,
            is_reparse_point=detector,
        )
    assert called is False


def test_inventory_allow_root_rejects_mocked_reparse_before_resolution(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    allowed = project / "data" / "validation"
    allowed.mkdir(parents=True)
    with pytest.raises(ProvenanceError, match="reparse"):
        validate_allowed_evidence_root(
            project,
            allowed,
            is_reparse_point=lambda path: path.name == "validation",
        )


def test_inventory_allow_root_rejects_outside_and_ambiguous_targets(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ProvenanceError, match="outside project"):
        validate_allowed_evidence_root(project, outside)
    with pytest.raises(ProvenanceError, match="ambiguous"):
        validate_allowed_evidence_root(project, project / "missing")


def test_false_attestation_and_unknown_manifest_fields_fail_closed(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    with pytest.raises(ProvenanceError, match="attestation"):
        build_baseline_manifest(
            root,
            approved_by="Jayant Saxena",
            sealed_test_exclusion_attested=False,
        )
    manifest = build_baseline_manifest(
        root,
        approved_by="Jayant Saxena",
        sealed_test_exclusion_attested=True,
    )
    manifest["unexpected"] = True
    with pytest.raises(ProvenanceError, match="schema"):
        write_manifest(root, manifest)


def test_manifest_output_is_fixed_and_cannot_be_overwritten(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    manifest = build_baseline_manifest(
        root,
        approved_by="Jayant Saxena",
        sealed_test_exclusion_attested=True,
    )
    output = write_manifest(root, manifest)
    with pytest.raises(ProvenanceError, match="already exists"):
        write_manifest(root, manifest)
    assert output.read_bytes() == canonical_json_bytes(manifest)


def test_manifest_output_reparse_fails_closed(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    artifacts = root / "artifacts"
    artifacts.mkdir()
    link = artifacts / "provenance"
    if os.name == "nt":
        created = subprocess.run(
            ("cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)),
            check=False,
            capture_output=True,
            text=True,
        )
        assert created.returncode == 0, created.stderr or created.stdout
    else:
        link.symlink_to(outside, target_is_directory=True)
    manifest = build_baseline_manifest(
        root,
        approved_by="Jayant Saxena",
        sealed_test_exclusion_attested=True,
    )
    try:
        with pytest.raises(ProvenanceError, match="reparse"):
            write_manifest(root, manifest)
        assert list(outside.iterdir()) == []
    finally:
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()


def _inventory_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "build_allowed_evidence_inventory.py"
    spec = importlib.util.spec_from_file_location("p0_inventory", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inventory_tree(root: Path) -> None:
    for relative in (
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
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "data" / "README.md").write_text("evidence\n", encoding="utf-8")
    (root / "data" / "raw" / "tick.json").write_text("{}\n", encoding="utf-8")


def test_inventory_is_reproducible_and_rejects_missing_or_changed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _inventory_module()
    _inventory_tree(tmp_path)
    first = module.build_inventory(tmp_path)
    second = module.build_inventory(tmp_path)
    assert first == second
    original_open = module.os.open

    def unreadable(path, flags, *args):
        if Path(path).name == "tick.json":
            raise PermissionError("fixture")
        return original_open(path, flags, *args)

    monkeypatch.setattr(module.os, "open", unreadable)
    with pytest.raises(ProvenanceError, match="unreadable"):
        module.build_inventory(tmp_path)
    monkeypatch.setattr(module.os, "open", original_open)
    shutil.rmtree(tmp_path / "data" / "raw")
    with pytest.raises(ProvenanceError, match="ambiguous|missing"):
        module.build_inventory(tmp_path)


def test_inventory_rejects_sealed_and_reparse_roots(tmp_path: Path) -> None:
    module = _inventory_module()
    _inventory_tree(tmp_path)
    sealed = tmp_path / "data" / "evaluation" / "development" / "sealed"
    sealed.mkdir()
    (sealed / "canary").write_text("do-not-read", encoding="utf-8")
    with pytest.raises(ProvenanceError, match="sealed"):
        module.build_inventory(tmp_path)


@pytest.mark.parametrize("mutation", ("late_add", "rename"))
def test_inventory_rejects_late_tree_races_without_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    module = _inventory_module()
    _inventory_tree(tmp_path)
    original_hash = module._hash_regular_file
    changed = False

    def racing_hash(path: Path):
        nonlocal changed
        result = original_hash(path)
        if not changed:
            changed = True
            raw = tmp_path / "data" / "raw"
            if mutation == "late_add":
                (raw / "late.json").write_text("{}\n", encoding="utf-8")
            else:
                (raw / "tick.json").rename(raw / "renamed.json")
        return result

    monkeypatch.setattr(module, "_hash_regular_file", racing_hash)
    with pytest.raises(
        ProvenanceError,
        match="tree changed|changed directory|unreadable file",
    ):
        module.build_inventory(tmp_path)
    assert not (tmp_path / ".p0-inventory").exists()


@pytest.mark.parametrize("mutation", ("rename", "substitute"))
def test_inventory_rejects_late_direct_file_root_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    module = _inventory_module()
    _inventory_tree(tmp_path)
    readme = tmp_path / "data" / "README.md"
    original_hash = module._hash_regular_file
    changed = False

    def racing_hash(path: Path):
        nonlocal changed
        result = original_hash(path)
        if path == readme and not changed:
            changed = True
            if mutation == "rename":
                readme.rename(readme.with_name("README-renamed.md"))
            else:
                readme.unlink()
                readme.write_text("substituted\n", encoding="utf-8")
        return result

    monkeypatch.setattr(module, "_hash_regular_file", racing_hash)
    with pytest.raises(
        ProvenanceError,
        match="changed direct-file root",
    ):
        module.build_inventory(tmp_path)
    assert not (tmp_path / ".p0-inventory").exists()


def test_inventory_rejects_actual_junction_without_output(tmp_path: Path) -> None:
    module = _inventory_module()
    _inventory_tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "data" / "raw" / f"junction-{uuid.uuid4().hex}"
    if os.name == "nt":
        created = subprocess.run(
            ("cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)),
            check=False,
            capture_output=True,
            text=True,
        )
        assert created.returncode == 0, created.stderr or created.stdout
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ProvenanceError, match="reparse"):
            module.build_inventory(tmp_path)
        assert not (tmp_path / ".p0-inventory").exists()
    finally:
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()


def test_inventory_rejects_unexpected_type_and_partial_traversal_without_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _inventory_module()
    _inventory_tree(tmp_path)
    original_scan = module._scan_directory

    def unexpected_type(path: Path):
        if path == tmp_path / "data" / "raw":
            raise ProvenanceError(
                f"INVENTORY_REJECTED: unexpected file type {path / 'fixture'}"
            )
        return original_scan(path)

    monkeypatch.setattr(module, "_scan_directory", unexpected_type)
    with pytest.raises(ProvenanceError, match="unexpected file type"):
        module.build_inventory(tmp_path)
    assert not (tmp_path / ".p0-inventory").exists()

    monkeypatch.setattr(module, "_scan_directory", original_scan)
    original_hash = module._hash_regular_file
    calls = 0

    def partial_failure(path: Path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ProvenanceError("INVENTORY_REJECTED: partial traversal fixture")
        return original_hash(path)

    monkeypatch.setattr(module, "_hash_regular_file", partial_failure)
    with pytest.raises(ProvenanceError, match="partial traversal"):
        module.build_inventory(tmp_path)
    assert not (tmp_path / ".p0-inventory").exists()


def test_inventory_cli_has_no_output_or_root_substitution() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "build_allowed_evidence_inventory.py"
    for injected in ("--output", "--root"):
        result = subprocess.run(
            (os.fspath(Path(sys.executable)), os.fspath(script), injected, "outside"),
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0


def test_inventory_existing_pair_is_verified_and_changed_pair_is_rejected(
    tmp_path: Path,
) -> None:
    module = _inventory_module()
    _inventory_tree(tmp_path)
    payload = module.build_inventory(tmp_path)
    expected = module.write_inventory("fixture", payload, tmp_path)
    assert module.write_inventory("fixture", payload, tmp_path) == expected

    output = (
        tmp_path
        / ".p0-inventory"
        / "remediation"
        / "fixture-evidence-hashes.jsonl"
    )
    output.write_bytes(output.read_bytes() + b"{}\r\n")
    with pytest.raises(ProvenanceError, match="changed existing inventory"):
        module.write_inventory("fixture", payload, tmp_path)


def test_manifest_cli_has_no_output_or_root_substitution() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "build_baseline_manifest.py"
    for injected in ("--output", "--root"):
        result = subprocess.run(
            (os.fspath(Path(sys.executable)), os.fspath(script), injected, "outside"),
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
