from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools" / "development_governance"
SIGNER = TOOLS / "sign_change_card.mjs"
VERIFIER = TOOLS / "verify_change_card.mjs"
COMMON = TOOLS / "change_card_common.mjs"
TEST_ROOT = ROOT / "docs" / "stage1_p0" / "patches" / "P0-A" / "test-only"
HASH64 = "a" * 64
HASH40 = "b" * 40


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _card() -> dict[str, object]:
    review_hashes = {
        "owner_authorization": HASH64,
        "plan_review": HASH64,
        "review_1": HASH64,
        "review_2": HASH64,
        "review_3": HASH64,
    }
    test_hashes = {
        key: HASH64
        for key in (
            "clean_checkout",
            "dashboard",
            "environment",
            "full_python",
            "inventory",
            "paper_only",
            "rollback",
            "secret_scan",
            "targeted_python",
        )
    }
    return {
        "approved_plan_hash": HASH64,
        "changed_paths": ["src/stage1/validation/provenance.py"],
        "code_commit": HASH40,
        "created_at": "2026-07-28T01:02:03+05:30",
        "environment_hash": HASH64,
        "excluded_paths": [
            ".env",
            ".p0-inventory",
            "artifacts/provenance",
            "credentials",
            "data/evaluation/sealed",
            "data/raw",
            "logs",
            "state",
        ],
        "inventory_hashes": {"after": HASH64, "before": HASH64},
        "owner_approval_reference":
            "docs/stage1_p0/patches/P0-A/08-owner-remediation-authorization.md",
        "patch_id": "P0-A",
        "plan_hash": HASH64,
        "previous_change_card_hash": "0" * 64,
        "prospective_from_session": "2026-07-29",
        "review_hashes": review_hashes,
        "reviewers": ["Hooke", "Kepler", "Pasteur"],
        "rollback_commit": "95441754d2f3ba5390a5fa1879a68deeb34ba7c6",
        "schema_versions_after": {"baseline_manifest": "baseline-manifest-v1"},
        "schema_versions_before": {"baseline_manifest": "none"},
        "synthesis_hash": HASH64,
        "synthesis_status": "PASS",
        "test_commands": [
            "python -m pytest tests/test_provenance.py tests/test_development_governance_signing.py -q",
            "python -m pytest -q",
            "python scripts/scan_secrets.py",
            "python scripts/verify_environment.py",
            "python -m pytest tests/test_paper_only_boundary.py tests/test_config.py -q",
            "npm.cmd test --prefix dashboard",
            "python scripts/build_allowed_evidence_inventory.py --label remediation-after",
            "git clean-checkout reproduction",
            "git revert rollback demonstration",
        ],
        "test_output_hashes": test_hashes,
        "tree_hash": HASH40,
    }


def _make_test_root() -> tuple[str, Path]:
    test_id = uuid.uuid4().hex
    root = TEST_ROOT / test_id
    root.mkdir(parents=True)
    (root / "16-change-card.json").write_bytes(_canonical(_card()))
    return test_id, root


def _run_node(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("node", str(script), *args),
        check=False,
        capture_output=True,
        text=True,
    )


def _run_common_eval(body: str, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    source = (
        f'import * as common from {json.dumps(COMMON.as_uri())};'
        'import {readFileSync} from "node:fs";'
        'const payload=JSON.parse(readFileSync(0,"utf8"));'
        f"{body}"
    )
    return subprocess.run(
        ("node", "--input-type=module", "-e", source),
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )


def _ephemeral_sign(test_id: str) -> subprocess.CompletedProcess[str]:
    return _run_node(SIGNER, "--ephemeral-test-sign", test_id)


def _verify(test_id: str) -> subprocess.CompletedProcess[str]:
    return _run_node(VERIFIER, "--verify-test", test_id)


def _other_public_key() -> str:
    wrapped = subprocess.run(
        ("node", str(SIGNER), "--generate-wrapped"),
        input="1" * 64,
        check=False,
        capture_output=True,
        text=True,
    )
    assert wrapped.returncode == 0, wrapped.stderr
    return str(json.loads(wrapped.stdout)["publicKey"])


def test_ephemeral_sign_verify_and_no_private_output() -> None:
    test_id, root = _make_test_root()
    try:
        signed = _ephemeral_sign(test_id)
        assert signed.returncode == 0, signed.stderr
        assert "PRIVATE KEY" not in (signed.stdout + signed.stderr)
        assert not any("private" in path.name.casefold() for path in root.iterdir())
        verified = _verify(test_id)
        assert verified.returncode == 0, verified.stderr
        assert json.loads(verified.stdout)["status"] == "VERIFIED"
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize(
    "mutation",
    ("unknown", "missing", "noncanonical_json", "nonpass", "unsafe_path"),
)
def test_strict_schema_and_canonical_card_rejection(mutation: str) -> None:
    test_id, root = _make_test_root()
    card_path = root / "16-change-card.json"
    card = _card()
    if mutation == "unknown":
        card["unexpected"] = True
    elif mutation == "missing":
        del card["patch_id"]
    elif mutation == "noncanonical_json":
        card_path.write_text(json.dumps(card, indent=2), encoding="utf-8")
    elif mutation == "nonpass":
        card["synthesis_status"] = "FAIL"
    else:
        card["changed_paths"] = ["../escape"]
    if mutation != "noncanonical_json":
        card_path.write_bytes(_canonical(card))
    try:
        result = _ephemeral_sign(test_id)
        assert result.returncode != 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize(
    "signature_text",
    (
        "not-base64",
        "A" * 88,
        base64.b64encode(b"x" * 63).decode("ascii"),
        base64.b64encode(b"x" * 64).decode("ascii") + "\n",
    ),
)
def test_malformed_or_noncanonical_base64_rejected(signature_text: str) -> None:
    test_id, root = _make_test_root()
    try:
        assert _ephemeral_sign(test_id).returncode == 0
        (root / "16-change-card.sig").write_text(signature_text, encoding="ascii")
        assert _verify(test_id).returncode != 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_mismatched_public_key_and_changed_card_rejected() -> None:
    test_id, root = _make_test_root()
    try:
        assert _ephemeral_sign(test_id).returncode == 0
        (root / "public.pem").write_text(_other_public_key(), encoding="ascii")
        assert _verify(test_id).returncode != 0
    finally:
        shutil.rmtree(root, ignore_errors=True)

    test_id, root = _make_test_root()
    try:
        assert _ephemeral_sign(test_id).returncode == 0
        card = _card()
        card["prospective_from_session"] = "2026-07-30"
        (root / "16-change-card.json").write_bytes(_canonical(card))
        assert _verify(test_id).returncode != 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_signature_bit_flip_is_rejected() -> None:
    test_id, root = _make_test_root()
    try:
        assert _ephemeral_sign(test_id).returncode == 0
        signature_path = root / "16-change-card.sig"
        raw = bytearray(base64.b64decode(signature_path.read_text(encoding="ascii")))
        raw[17] ^= 0x01
        signature_path.write_text(
            base64.b64encode(raw).decode("ascii"),
            encoding="ascii",
        )
        assert _verify(test_id).returncode != 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize("test_id", ("../escape", "A" * 32, "0" * 31, "C:/absolute"))
def test_test_path_injection_rejected(test_id: str) -> None:
    assert _run_node(SIGNER, "--ephemeral-test-sign", test_id).returncode != 0
    assert _run_node(VERIFIER, "--verify-test", test_id).returncode != 0


def test_fixed_credential_and_output_constraints() -> None:
    credential_script = TOOLS / "credential_store.ps1"
    command = (
        f". '{credential_script}'; "
        "Assert-Stage1AllowedCredentialTarget -Target 'Substituted/Target'"
    )
    rejected = subprocess.run(
        ("powershell.exe", "-NoProfile", "-Command", command),
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    for wrapper in (
        "setup_ed25519_key.ps1",
        "sign_change_card.ps1",
        "verify_change_card.ps1",
        "remove_ed25519_key.ps1",
    ):
        text = (TOOLS / wrapper).read_text(encoding="utf-8")
        declaration = text.splitlines()[0]
        assert "PublicKeyPath" not in declaration
        assert "SignaturePath" not in declaration
        assert "CardPath" not in declaration
        assert "$Target" not in declaration


@pytest.mark.skipif(os.name != "nt", reason="Windows Credential Manager required")
def test_isolated_testonly_credential_lifecycle_and_no_private_output() -> None:
    test_id = uuid.uuid4().hex
    root = TEST_ROOT / test_id
    setup = TOOLS / "setup_ed25519_key.ps1"
    remove = TOOLS / "remove_ed25519_key.ps1"
    try:
        created = subprocess.run(
            (
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(setup),
                "-TestId",
                test_id,
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        assert created.returncode == 0, created.stderr
        assert json.loads(created.stdout)["status"] == "CREATED"
        assert "PRIVATE KEY" not in (created.stdout + created.stderr)
        assert (root / "public.pem").is_file()
        assert not any("private" in path.name.casefold() for path in root.iterdir())
    finally:
        removed = subprocess.run(
            (
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(remove),
                "-TestId",
                test_id,
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        assert removed.returncode == 0, removed.stderr
        assert "ABSENT" in removed.stdout
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows Credential Manager required")
def test_authenticated_transport_tamper_fails_closed() -> None:
    test_id = uuid.uuid4().hex
    root = TEST_ROOT / test_id
    setup = TOOLS / "setup_ed25519_key.ps1"
    remove = TOOLS / "remove_ed25519_key.ps1"
    created = subprocess.run(
        (
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(setup),
            "-TestId",
            test_id,
            "-TestTamperTransportTag",
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        assert created.returncode != 0
        assert "ED25519_TRANSPORT_AUTHENTICATION_FAILED" in created.stderr
        assert "PRIVATE KEY" not in (created.stdout + created.stderr)
        assert not (root / "public.pem").exists()
    finally:
        removed = subprocess.run(
            (
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(remove),
                "-TestId",
                test_id,
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        assert removed.returncode == 0, removed.stderr
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows Credential Manager required")
@pytest.mark.parametrize("fail_after_replace", (False, True))
def test_existing_public_replacement_restoration_and_cleanup(
    fail_after_replace: bool,
) -> None:
    test_id = uuid.uuid4().hex
    root = TEST_ROOT / test_id
    root.mkdir(parents=True)
    public_path = root / "public.pem"
    original = b"ORIGINAL-PUBLIC-PLACEHOLDER\r\n"
    public_path.write_bytes(original)
    setup = TOOLS / "setup_ed25519_key.ps1"
    remove = TOOLS / "remove_ed25519_key.ps1"
    arguments = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(setup),
        "-TestId",
        test_id,
        "-TestAllowExistingPublic",
    ]
    if fail_after_replace:
        arguments.append("-TestFailAfterPublicReplacement")
    result = subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        if fail_after_replace:
            assert result.returncode != 0
            assert "TEST_POST_REPLACEMENT_FAILURE" in result.stderr
            assert public_path.read_bytes() == original
        else:
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout)["status"] == "CREATED"
            assert public_path.read_bytes() != original
            assert "BEGIN PUBLIC KEY" in public_path.read_text(encoding="ascii")
        assert not list(root.glob(".research-signing-*.tmp"))
        assert "PRIVATE KEY" not in (result.stdout + result.stderr)
    finally:
        removed = subprocess.run(
            (
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(remove),
                "-TestId",
                test_id,
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        assert removed.returncode == 0, removed.stderr
        shutil.rmtree(root, ignore_errors=True)


def test_testonly_reparse_directory_is_rejected(tmp_path: Path) -> None:
    test_id = uuid.uuid4().hex
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = TEST_ROOT / test_id
    junction_created = False
    try:
        if os.name == "nt":
            created = subprocess.run(
                ("cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)),
                check=False,
                capture_output=True,
                text=True,
            )
            assert created.returncode == 0, created.stderr or created.stdout
            junction_created = True
        else:
            link.symlink_to(outside, target_is_directory=True)
        (outside / "16-change-card.json").write_bytes(_canonical(_card()))
        result = _ephemeral_sign(test_id)
        assert result.returncode != 0
        assert not (outside / "public.pem").exists()
        assert not (outside / "16-change-card.sig").exists()
    finally:
        if junction_created:
            os.rmdir(link)
        elif link.is_symlink():
            link.unlink()


def test_private_transport_is_stdin_only_and_not_environment_or_argument() -> None:
    signer_text = SIGNER.read_text(encoding="utf-8")
    setup_text = (TOOLS / "setup_ed25519_key.ps1").read_text(encoding="utf-8")
    assert '--generate-wrapped $transportHex' not in setup_text
    assert '$transportHex | & node $NodeScript --generate-wrapped' in setup_text
    assert "process.env" not in signer_text
    assert "--check-pair" in signer_text


def test_mismatched_private_public_pair_is_rejected_without_private_output() -> None:
    result = _run_node(SIGNER, "--ephemeral-test-mismatched-pair")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "MISMATCH_REJECTED"
    assert "PRIVATE KEY" not in (result.stdout + result.stderr)


@pytest.mark.parametrize("mutation", ("command", "transcript", "unknown"))
def test_pass_record_is_bound_to_fixed_command_and_transcript(mutation: str) -> None:
    transcript = b"trusted command output\r\n"
    transcript_hash = __import__("hashlib").sha256(transcript).hexdigest()
    record: dict[str, object] = {
        "command": "python scripts/verify_environment.py",
        "exit_code": 0,
        "status": "PASS",
        "transcript_sha256": transcript_hash,
    }
    supplied_transcript = transcript
    if mutation == "command":
        record["command"] = "python substituted.py"
    elif mutation == "transcript":
        supplied_transcript = b"different output\r\n"
    else:
        record["unverified"] = True
    payload = {
        "raw": _canonical(record).decode("utf-8"),
        "transcript": base64.b64encode(supplied_transcript).decode("ascii"),
    }
    result = _run_common_eval(
        'common.validatePassRecordBytes('
        'payload.raw,Buffer.from(payload.transcript,"base64"),'
        '"python scripts/verify_environment.py","ENVIRONMENT");',
        payload,
    )
    assert result.returncode != 0


@pytest.mark.parametrize(
    "text",
    (
        "Decision: `FAIL`\n",
        "Decision: `PASS`\nDecision: `FAIL`\n",
        "A sentence containing PASS but no decision.\n",
    ),
)
def test_review_decision_requires_one_exact_pass_line(text: str) -> None:
    result = _run_common_eval(
        'common.validateMarkdownDecisionText(payload.text,"REVIEW");',
        {"text": text},
    )
    assert result.returncode != 0


def test_preserved_inventory_requires_fixed_hash_and_record_count() -> None:
    inventory = (
        ROOT
        / ".p0-inventory"
        / "remediation"
        / "remediation-before-evidence-hashes.jsonl"
    ).read_bytes()
    preserved = "1b13f37fc9204d8e8eef23c9fc657446e4ff28752569bd5ba6cf8c25a7656349"
    valid = _run_common_eval(
        'common.validatePreservedInventoryBytes('
        'Buffer.from(payload.raw,"base64"),payload.hash);',
        {"raw": base64.b64encode(inventory).decode("ascii"), "hash": preserved},
    )
    assert valid.returncode == 0, valid.stderr
    for changed in (inventory[:-2], inventory + b"{}\r\n"):
        rejected = _run_common_eval(
            'common.validatePreservedInventoryBytes('
            'Buffer.from(payload.raw,"base64"),payload.hash);',
            {"raw": base64.b64encode(changed).decode("ascii"), "hash": preserved},
        )
        assert rejected.returncode != 0


@pytest.mark.parametrize("mutation", ("commit", "inventory", "transcript"))
def test_rollback_proof_is_cross_bound(mutation: str) -> None:
    import hashlib

    transcript = b"safe rollback transcript\r\n"
    inventory_hash = (
        "1b13f37fc9204d8e8eef23c9fc657446e4ff28752569bd5ba6cf8c25a7656349"
    )
    card = _card()
    record: dict[str, object] = {
        "command": "git revert rollback demonstration",
        "evidence_inventory_sha256": inventory_hash,
        "exit_code": 0,
        "excluded_canaries_preserved": True,
        "governance_preserved": True,
        "p0_commit": card["code_commit"],
        "pre_p0_tree": "0477f408cf302377f3632c4b933a9e8e7f1e4503",
        "reverted_tree": "0477f408cf302377f3632c4b933a9e8e7f1e4503",
        "status": "PASS",
        "transcript_sha256": hashlib.sha256(transcript).hexdigest(),
    }
    supplied_transcript = transcript
    if mutation == "commit":
        record["p0_commit"] = "c" * 40
    elif mutation == "inventory":
        record["evidence_inventory_sha256"] = "d" * 64
    else:
        supplied_transcript = b"forged transcript\r\n"
    result = _run_common_eval(
        'common.validateRollbackProofRecord('
        'payload.raw,Buffer.from(payload.transcript,"base64"),'
        'payload.card,payload.inventory);',
        {
            "raw": _canonical(record).decode("utf-8"),
            "transcript": base64.b64encode(supplied_transcript).decode("ascii"),
            "card": card,
            "inventory": inventory_hash,
        },
    )
    assert result.returncode != 0


@pytest.mark.parametrize(
    "mutation",
    ("head", "tree", "parent", "pre_p0_tree", "changed_paths", "created_at", "unknown"),
)
def test_forged_or_stale_git_metadata_is_rejected(mutation: str) -> None:
    card = _card()
    observed: dict[str, object] = {
        "changed_paths": card["changed_paths"],
        "created_at": card["created_at"],
        "head": card["code_commit"],
        "parent": "95441754d2f3ba5390a5fa1879a68deeb34ba7c6",
        "pre_p0_tree": "0477f408cf302377f3632c4b933a9e8e7f1e4503",
        "tree": card["tree_hash"],
    }
    if mutation in {"head", "tree"}:
        observed[mutation] = "c" * 40
    elif mutation == "parent":
        observed[mutation] = "d" * 40
    elif mutation == "pre_p0_tree":
        observed[mutation] = "e" * 40
    elif mutation == "changed_paths":
        observed[mutation] = ["forged/path.py"]
    elif mutation == "created_at":
        observed[mutation] = "2026-07-28T01:02:04+05:30"
    else:
        observed["unverified"] = True
    result = _run_common_eval(
        "common.validateObservedGitAssertions(payload.card,payload.observed);",
        {"card": card, "observed": observed},
    )
    assert result.returncode != 0


def test_wrapped_generation_does_not_leak_private_material_or_environment_canary() -> None:
    canary = f"P0A-CANARY-{uuid.uuid4().hex}"
    environment = os.environ.copy()
    environment["P0A_TEST_SECRET_CANARY"] = canary
    wrapped = subprocess.run(
        ("node", str(SIGNER), "--generate-wrapped"),
        input="2" * 64,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert wrapped.returncode == 0, wrapped.stderr
    combined = wrapped.stdout + wrapped.stderr
    assert canary not in combined
    assert "PRIVATE KEY" not in combined
    assert set(json.loads(wrapped.stdout)) == {
        "encryptedPrivateKey",
        "fingerprint",
        "iv",
        "publicKey",
        "tag",
    }


def test_runtime_has_no_development_governance_import() -> None:
    violations = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src" / "stage1").rglob("*.py")
        if "development_governance" in path.read_text(encoding="utf-8")
    ]
    assert violations == []


def test_governance_tools_do_not_reference_broker_or_telegram_credentials() -> None:
    forbidden = ("FYERS_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN", "place_order")
    for path in TOOLS.iterdir():
        if path.suffix.lower() in {".ps1", ".mjs"}:
            text = path.read_text(encoding="utf-8")
            assert all(marker not in text for marker in forbidden)
