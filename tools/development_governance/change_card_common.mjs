import {
  createHash,
  createPrivateKey,
  createPublicKey,
} from "node:crypto";
import {
  existsSync,
  lstatSync,
  readFileSync,
  realpathSync,
} from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

export const PROJECT_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
export const PATCH_RELATIVE = "docs/stage1_p0/patches/P0-A";
export const PUBLIC_KEY_RELATIVE = "security/research-signing-ed25519.pub.pem";
export const CARD_RELATIVE = `${PATCH_RELATIVE}/16-change-card.json`;
export const SIGNATURE_RELATIVE = `${PATCH_RELATIVE}/16-change-card.sig`;
export const PERSISTENT_TARGET =
  "Stage1PaperAgent/P0/DevelopmentChangeCard/Ed25519/v1";
export const PRE_P0_COMMIT = "95441754d2f3ba5390a5fa1879a68deeb34ba7c6";
export const PRE_P0_TREE = "0477f408cf302377f3632c4b933a9e8e7f1e4503";
export const ACCEPTED_PUBLIC_KEY_FINGERPRINT =
  "b0defe5a8504f69ce899072cc9ac8bb01875812cfc53838faa8aa95ce69d321a";

const TEST_ID = /^[0-9a-f]{32}$/;
const HASH_64 = /^[0-9a-f]{64}$/;
const HASH_40 = /^[0-9a-f]{40}$/;
const BASE64_64 = /^[A-Za-z0-9+/]{86}==$/;
const CARD_FIELDS = Object.freeze([
  "approved_plan_hash",
  "changed_paths",
  "code_commit",
  "created_at",
  "environment_hash",
  "excluded_paths",
  "inventory_hashes",
  "owner_approval_reference",
  "patch_id",
  "plan_hash",
  "previous_change_card_hash",
  "prospective_from_session",
  "review_hashes",
  "reviewers",
  "rollback_commit",
  "schema_versions_after",
  "schema_versions_before",
  "synthesis_hash",
  "synthesis_status",
  "test_commands",
  "test_output_hashes",
  "tree_hash",
]);
const REVIEW_KEYS = Object.freeze([
  "owner_authorization",
  "plan_review",
  "review_1",
  "review_2",
  "review_3",
]);
const INVENTORY_KEYS = Object.freeze(["after", "before"]);
const SCHEMA_VERSION_KEYS = Object.freeze(["baseline_manifest"]);
const TEST_KEYS = Object.freeze([
  "clean_checkout",
  "dashboard",
  "environment",
  "full_python",
  "inventory",
  "paper_only",
  "rollback",
  "secret_scan",
  "targeted_python",
]);
const GOVERNANCE_PATHS = Object.freeze({
  owner_authorization: `${PATCH_RELATIVE}/08-owner-remediation-authorization.md`,
  plan_review: `${PATCH_RELATIVE}/10-remediation-plan-review.md`,
  review_1: `${PATCH_RELATIVE}/12b-remediation-review-causal-data.md`,
  review_2: `${PATCH_RELATIVE}/13b-remediation-review-paper-security.md`,
  review_3: `${PATCH_RELATIVE}/14b-remediation-review-tests-rollback.md`,
});
const TEST_PATHS = Object.freeze({
  clean_checkout: ".p0-inventory/tests/P0-A-remediation/clean-checkout.txt",
  dashboard: ".p0-inventory/tests/P0-A-remediation/dashboard.txt",
  environment: ".p0-inventory/tests/P0-A-remediation/environment.txt",
  full_python: ".p0-inventory/tests/P0-A-remediation/python-full.txt",
  inventory: ".p0-inventory/tests/P0-A-remediation/inventory.txt",
  paper_only: ".p0-inventory/tests/P0-A-remediation/paper-only.txt",
  rollback: ".p0-inventory/tests/P0-A-remediation/rollback.txt",
  secret_scan: ".p0-inventory/tests/P0-A-remediation/secret-scan.txt",
  targeted_python: ".p0-inventory/tests/P0-A-remediation/python-targeted.txt",
});
const TRANSCRIPT_PATHS = Object.freeze({
  clean_checkout: ".p0-inventory/tests/P0-A-remediation/clean-checkout.transcript",
  dashboard: ".p0-inventory/tests/P0-A-remediation/dashboard.transcript",
  environment: ".p0-inventory/tests/P0-A-remediation/environment.transcript",
  full_python: ".p0-inventory/tests/P0-A-remediation/python-full.transcript",
  inventory: ".p0-inventory/tests/P0-A-remediation/inventory.transcript",
  paper_only: ".p0-inventory/tests/P0-A-remediation/paper-only.transcript",
  rollback: ".p0-inventory/tests/P0-A-remediation/rollback.transcript",
  secret_scan: ".p0-inventory/tests/P0-A-remediation/secret-scan.transcript",
  targeted_python: ".p0-inventory/tests/P0-A-remediation/python-targeted.transcript",
});
const FIXED_EXCLUDED_PATHS = Object.freeze([
  ".env",
  ".p0-inventory",
  "artifacts/provenance",
  "credentials",
  "data/evaluation/sealed",
  "data/raw",
  "logs",
  "state",
]);
const FIXED_REVIEWERS = Object.freeze(["Hooke", "Kepler", "Pasteur"]);
const FIXED_TEST_COMMANDS = Object.freeze([
  "python -m pytest tests/test_provenance.py tests/test_development_governance_signing.py -q",
  "python -m pytest -q",
  "python scripts/scan_secrets.py",
  "python scripts/verify_environment.py",
  "python -m pytest tests/test_paper_only_boundary.py tests/test_config.py -q",
  "npm.cmd test --prefix dashboard",
  "python scripts/build_allowed_evidence_inventory.py --label remediation-after",
  "git clean-checkout reproduction",
  "git revert rollback demonstration",
]);
const FIXED_TEST_COMMAND_BY_KEY = Object.freeze({
  targeted_python: FIXED_TEST_COMMANDS[0],
  full_python: FIXED_TEST_COMMANDS[1],
  secret_scan: FIXED_TEST_COMMANDS[2],
  environment: FIXED_TEST_COMMANDS[3],
  paper_only: FIXED_TEST_COMMANDS[4],
  dashboard: FIXED_TEST_COMMANDS[5],
  inventory: FIXED_TEST_COMMANDS[6],
  clean_checkout: FIXED_TEST_COMMANDS[7],
  rollback: FIXED_TEST_COMMANDS[8],
});
const FIXED_SCHEMA_BEFORE = Object.freeze({
  baseline_manifest: "none",
});
const FIXED_SCHEMA_AFTER = Object.freeze({
  baseline_manifest: "baseline-manifest-v1",
});
const FIXED_PROSPECTIVE_SESSION = "2026-07-29";
const PRESERVED_INVENTORY_SHA256 =
  "1b13f37fc9204d8e8eef23c9fc657446e4ff28752569bd5ba6cf8c25a7656349";
const PRESERVED_INVENTORY_RECORD_COUNT = 37602;

export function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) =>
      `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

export function canonicalBytes(value) {
  return Buffer.from(`${canonical(value)}\n`, "utf8");
}

function assertExactKeys(value, expected, label) {
  if (!value || Array.isArray(value) || typeof value !== "object") {
    throw new Error(`${label}_NOT_OBJECT`);
  }
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (actual.length !== wanted.length ||
      actual.some((key, index) => key !== wanted[index])) {
    throw new Error(`${label}_SCHEMA_INVALID`);
  }
}

function assertHash(value, length, label) {
  const pattern = length === 40 ? HASH_40 : HASH_64;
  if (typeof value !== "string" || !pattern.test(value)) {
    throw new Error(`${label}_INVALID`);
  }
}

function assertStringArray(value, label, { allowEmpty = true } = {}) {
  if (!Array.isArray(value) || (!allowEmpty && value.length === 0) ||
      value.some((item) => typeof item !== "string" || item.length === 0)) {
    throw new Error(`${label}_INVALID`);
  }
}

export function validateCardSchema(card) {
  assertExactKeys(card, CARD_FIELDS, "CARD");
  assertHash(card.approved_plan_hash, 64, "APPROVED_PLAN_HASH");
  assertHash(card.code_commit, 40, "CODE_COMMIT");
  assertHash(card.environment_hash, 64, "ENVIRONMENT_HASH");
  assertHash(card.plan_hash, 64, "PLAN_HASH");
  assertHash(card.previous_change_card_hash, 64, "PREVIOUS_CHANGE_CARD_HASH");
  assertHash(card.rollback_commit, 40, "ROLLBACK_COMMIT");
  assertHash(card.synthesis_hash, 64, "SYNTHESIS_HASH");
  assertHash(card.tree_hash, 40, "TREE_HASH");
  assertStringArray(card.changed_paths, "CHANGED_PATHS", { allowEmpty: false });
  assertStringArray(card.excluded_paths, "EXCLUDED_PATHS");
  assertStringArray(card.reviewers, "REVIEWERS", { allowEmpty: false });
  assertStringArray(card.test_commands, "TEST_COMMANDS", { allowEmpty: false });
  if (new Set(card.changed_paths).size !== card.changed_paths.length ||
      new Set(card.excluded_paths).size !== card.excluded_paths.length ||
      new Set(card.reviewers).size !== card.reviewers.length) {
    throw new Error("CARD_DUPLICATE_ARRAY_VALUE");
  }
  if (card.reviewers.length !== 3) throw new Error("REVIEWERS_INVALID");
  if (JSON.stringify(card.excluded_paths) !==
      JSON.stringify(FIXED_EXCLUDED_PATHS)) throw new Error("EXCLUDED_PATHS_UNBOUND");
  if (JSON.stringify(card.reviewers) !==
      JSON.stringify(FIXED_REVIEWERS)) throw new Error("REVIEWERS_UNBOUND");
  if (JSON.stringify(card.test_commands) !==
      JSON.stringify(FIXED_TEST_COMMANDS)) throw new Error("TEST_COMMANDS_UNBOUND");
  if (card.changed_paths.some((item) => !isSafeRelative(item)) ||
      card.excluded_paths.some((item) => !isSafeRelative(item))) {
    throw new Error("CARD_PATH_INVALID");
  }
  assertExactKeys(card.inventory_hashes, INVENTORY_KEYS, "INVENTORY_HASHES");
  for (const key of INVENTORY_KEYS) {
    assertHash(card.inventory_hashes[key], 64, `INVENTORY_${key.toUpperCase()}`);
  }
  assertExactKeys(card.review_hashes, REVIEW_KEYS, "REVIEW_HASHES");
  for (const key of REVIEW_KEYS) {
    assertHash(card.review_hashes[key], 64, `REVIEW_${key.toUpperCase()}`);
  }
  assertExactKeys(card.test_output_hashes, TEST_KEYS, "TEST_OUTPUT_HASHES");
  for (const key of TEST_KEYS) {
    assertHash(card.test_output_hashes[key], 64, `TEST_${key.toUpperCase()}`);
  }
  assertExactKeys(
    card.schema_versions_before, SCHEMA_VERSION_KEYS, "SCHEMA_VERSIONS_BEFORE",
  );
  assertExactKeys(
    card.schema_versions_after, SCHEMA_VERSION_KEYS, "SCHEMA_VERSIONS_AFTER",
  );
  for (const key of SCHEMA_VERSION_KEYS) {
    if (typeof card.schema_versions_before[key] !== "string" ||
        typeof card.schema_versions_after[key] !== "string") {
      throw new Error("SCHEMA_VERSIONS_INVALID");
    }
  }
  if (JSON.stringify(card.schema_versions_before) !==
      JSON.stringify(FIXED_SCHEMA_BEFORE) ||
      JSON.stringify(card.schema_versions_after) !==
      JSON.stringify(FIXED_SCHEMA_AFTER)) {
    throw new Error("SCHEMA_VERSIONS_UNBOUND");
  }
  if (card.patch_id !== "P0-A") throw new Error("PATCH_ID_INVALID");
  if (card.synthesis_status !== "PASS") throw new Error("CARD_NOT_PASS");
  if (card.rollback_commit !== PRE_P0_COMMIT) throw new Error("ROLLBACK_INVALID");
  if (card.owner_approval_reference !==
      `${PATCH_RELATIVE}/08-owner-remediation-authorization.md`) {
    throw new Error("OWNER_REFERENCE_INVALID");
  }
  if (card.previous_change_card_hash !== "0".repeat(64)) {
    throw new Error("PREVIOUS_CHANGE_CARD_HASH_INVALID");
  }
  if (typeof card.prospective_from_session !== "string" ||
      !/^\d{4}-\d{2}-\d{2}$/.test(card.prospective_from_session)) {
    throw new Error("PROSPECTIVE_SESSION_INVALID");
  }
  if (card.prospective_from_session !== FIXED_PROSPECTIVE_SESSION) {
    throw new Error("PROSPECTIVE_SESSION_UNBOUND");
  }
  if (typeof card.created_at !== "string" ||
      !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$/.test(
        card.created_at)) {
    throw new Error("CREATED_AT_INVALID");
  }
  return card;
}

export function isSafeRelative(value) {
  if (typeof value !== "string" || value.length === 0 || value.includes("\\") ||
      value.includes("\0") || isAbsolute(value)) return false;
  const parts = value.split("/");
  return parts.every((part) => part && part !== "." && part !== ".." &&
    !part.includes(":"));
}

function assertComponentSafety(root, target, { leafMustExist = true } = {}) {
  const absoluteRoot = resolve(root);
  const absoluteTarget = resolve(target);
  const boundary = relative(absoluteRoot, absoluteTarget);
  if (boundary === ".." || boundary.startsWith(`..${sep}`) ||
      isAbsolute(boundary)) throw new Error("PATH_ESCAPE");
  const rootStatus = lstatSync(absoluteRoot, { throwIfNoEntry: false });
  if (!rootStatus || !rootStatus.isDirectory() || isReparse(absoluteRoot, rootStatus)) {
    throw new Error("UNSAFE_ROOT");
  }
  let cursor = absoluteRoot;
  const parts = boundary ? boundary.split(sep) : [];
  for (let index = 0; index < parts.length; index += 1) {
    cursor = join(cursor, parts[index]);
    const status = lstatSync(cursor, { throwIfNoEntry: false });
    const isLeaf = index === parts.length - 1;
    if (!status) {
      if (isLeaf && !leafMustExist) return absoluteTarget;
      throw new Error("PATH_MISSING");
    }
    if (isReparse(cursor, status)) throw new Error("PATH_REPARSE_OR_SYMLINK");
    if (!isLeaf && !status.isDirectory()) throw new Error("PATH_COMPONENT_NOT_DIRECTORY");
  }
  const resolvedRoot = realpathSync(absoluteRoot);
  const resolvedTarget = realpathSync(absoluteTarget);
  const resolvedBoundary = relative(resolvedRoot, resolvedTarget);
  if (resolvedBoundary === ".." || resolvedBoundary.startsWith(`..${sep}`) ||
      isAbsolute(resolvedBoundary)) throw new Error("RESOLVED_PATH_ESCAPE");
  return absoluteTarget;
}

function isReparse(path, status) {
  if (status.isSymbolicLink()) return true;
  if (process.platform !== "win32") return false;
  const result = execFileSync(
    "powershell.exe",
    [
      "-NoProfile",
      "-NonInteractive",
      "-Command",
      "& { param([string]$p) " +
        "$i=Get-Item -LiteralPath $p -Force;" +
        "if (($i.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) " +
        "{'1'} else {'0'} }",
      path,
    ],
    { encoding: "utf8", windowsHide: true, stdio: ["ignore", "pipe", "pipe"] },
  ).trim();
  if (result !== "0" && result !== "1") throw new Error("REPARSE_STATUS_AMBIGUOUS");
  return result === "1";
}

export function fixedExistingFile(relativePath) {
  if (!isSafeRelative(relativePath)) throw new Error("FIXED_PATH_INVALID");
  const actual = assertComponentSafety(PROJECT_ROOT, join(PROJECT_ROOT, relativePath));
  const status = lstatSync(actual);
  if (!status.isFile() || status.isSymbolicLink()) throw new Error("NOT_REGULAR_FILE");
  return actual;
}

export function fixedNewFile(relativePath) {
  if (!isSafeRelative(relativePath)) throw new Error("FIXED_PATH_INVALID");
  const actual = join(PROJECT_ROOT, relativePath);
  if (existsSync(actual)) throw new Error("OUTPUT_EXISTS");
  assertComponentSafety(PROJECT_ROOT, dirname(actual));
  return actual;
}

export function testPaths(testId, { requireExisting = false } = {}) {
  if (!TEST_ID.test(testId)) throw new Error("TEST_ID_INVALID");
  const rootRelative = `${PATCH_RELATIVE}/test-only/${testId}`;
  const root = requireExisting
    ? assertComponentSafety(PROJECT_ROOT, join(PROJECT_ROOT, rootRelative))
    : join(PROJECT_ROOT, rootRelative);
  return {
    root,
    rootRelative,
    card: join(root, "16-change-card.json"),
    signature: join(root, "16-change-card.sig"),
    publicKey: join(root, "public.pem"),
  };
}

export function readCanonicalCard(path) {
  const status = lstatSync(path);
  if (!status.isFile() || status.isSymbolicLink()) throw new Error("CARD_NOT_REGULAR");
  const raw = readFileSync(path);
  let parsed;
  try {
    parsed = JSON.parse(raw.toString("utf8"));
  } catch {
    throw new Error("CARD_JSON_INVALID");
  }
  validateCardSchema(parsed);
  const expected = canonicalBytes(parsed);
  if (!raw.equals(expected)) throw new Error("CARD_NOT_CANONICAL");
  return { card: parsed, bytes: expected };
}

export function strictSignatureBytes(text) {
  if (typeof text !== "string" || !BASE64_64.test(text)) {
    throw new Error("SIGNATURE_BASE64_INVALID");
  }
  const decoded = Buffer.from(text, "base64");
  if (decoded.length !== 64 || decoded.toString("base64") !== text) {
    decoded.fill(0);
    throw new Error("SIGNATURE_BASE64_NONCANONICAL");
  }
  return decoded;
}

export function publicFingerprint(publicKey) {
  const der = createPublicKey(publicKey).export({ type: "spki", format: "der" });
  return createHash("sha256").update(der).digest("hex");
}

export function assertKeyCorrespondence(privatePem, publicPem) {
  const privateKey = createPrivateKey(privatePem);
  if (privateKey.asymmetricKeyType !== "ed25519") throw new Error("PRIVATE_KEY_NOT_ED25519");
  const expected = createPublicKey(privateKey).export({ type: "spki", format: "der" });
  const providedKey = createPublicKey(publicPem);
  if (providedKey.asymmetricKeyType !== "ed25519") throw new Error("PUBLIC_KEY_NOT_ED25519");
  const provided = providedKey.export({ type: "spki", format: "der" });
  if (!Buffer.from(expected).equals(Buffer.from(provided))) {
    throw new Error("KEY_PAIR_MISMATCH");
  }
  return privateKey;
}

function sha256File(relativePath) {
  return createHash("sha256").update(readFileSync(fixedExistingFile(relativePath))).digest("hex");
}

export function validatePassRecordBytes(
  raw,
  transcriptBytes,
  expectedCommand,
  label,
) {
  if (typeof raw !== "string" || !Buffer.isBuffer(transcriptBytes)) {
    throw new Error(`${label}_RECORD_INVALID`);
  }
  let record;
  try {
    record = JSON.parse(raw);
  } catch {
    throw new Error(`${label}_RECORD_INVALID`);
  }
  const keys = Object.keys(record).sort();
  const expected = ["command", "exit_code", "status", "transcript_sha256"];
  if (JSON.stringify(keys) !== JSON.stringify(expected) ||
      record.status !== "PASS" || record.exit_code !== 0 ||
      record.command !== expectedCommand ||
      typeof record.transcript_sha256 !== "string" ||
      !HASH_64.test(record.transcript_sha256) ||
      raw !== `${canonical(record)}\n`) {
    throw new Error(`${label}_NOT_PASS`);
  }
  const actualTranscriptHash =
    createHash("sha256").update(transcriptBytes).digest("hex");
  if (record.transcript_sha256 !== actualTranscriptHash) {
    throw new Error(`${label}_TRANSCRIPT_UNBOUND`);
  }
  return record;
}

function readPassRecord(relativePath, transcriptPath, expectedCommand, label) {
  return validatePassRecordBytes(
    readFileSync(fixedExistingFile(relativePath), "utf8"),
    readFileSync(fixedExistingFile(transcriptPath)),
    expectedCommand,
    label,
  );
}

export function validateMarkdownDecisionText(rawText, label) {
  if (typeof rawText !== "string") throw new Error(`${label}_NOT_PASS`);
  const text = rawText.replace(/\r\n/g, "\n");
  const decisions = text.split("\n").filter((line) =>
    /^Decision: `(PASS|FAIL)`$/.test(line));
  if (decisions.length !== 1 || decisions[0] !== "Decision: `PASS`") {
    throw new Error(`${label}_NOT_PASS`);
  }
}

function requireMarkdownDecision(relativePath, label) {
  validateMarkdownDecisionText(
    readFileSync(fixedExistingFile(relativePath), "utf8"),
    label,
  );
}

export function validateRollbackProofRecord(
  raw,
  transcriptBytes,
  card,
  inventoryHash,
) {
  if (typeof raw !== "string" || !Buffer.isBuffer(transcriptBytes)) {
    throw new Error("ROLLBACK_RECORD_INVALID");
  }
  let record;
  try {
    record = JSON.parse(raw);
  } catch {
    throw new Error("ROLLBACK_RECORD_INVALID");
  }
  const expected = [
    "command",
    "evidence_inventory_sha256",
    "exit_code",
    "excluded_canaries_preserved",
    "governance_preserved",
    "p0_commit",
    "pre_p0_tree",
    "reverted_tree",
    "status",
    "transcript_sha256",
  ].sort();
  if (JSON.stringify(Object.keys(record).sort()) !== JSON.stringify(expected) ||
      raw !== `${canonical(record)}\n` || record.status !== "PASS" ||
      record.command !== FIXED_TEST_COMMAND_BY_KEY.rollback ||
      record.exit_code !== 0 ||
      record.pre_p0_tree !== PRE_P0_TREE || record.reverted_tree !== PRE_P0_TREE ||
      record.excluded_canaries_preserved !== true ||
      record.governance_preserved !== true ||
      record.p0_commit !== card.code_commit ||
      record.evidence_inventory_sha256 !== inventoryHash ||
      !HASH_64.test(record.transcript_sha256) ||
      record.transcript_sha256 !==
        createHash("sha256").update(transcriptBytes).digest("hex")) {
    throw new Error("ROLLBACK_NOT_PROVEN");
  }
  return record;
}

function requireRollbackProof(card, inventoryHash) {
  validateRollbackProofRecord(
    readFileSync(fixedExistingFile(TEST_PATHS.rollback), "utf8"),
    readFileSync(fixedExistingFile(TRANSCRIPT_PATHS.rollback)),
    card,
    inventoryHash,
  );
}

export function validatePreservedInventoryBytes(raw, expectedHash) {
  if (!Buffer.isBuffer(raw) || typeof expectedHash !== "string") {
    throw new Error("INVENTORY_BASELINE_MISMATCH");
  }
  const hash = createHash("sha256").update(raw).digest("hex");
  if (hash !== expectedHash || hash !== PRESERVED_INVENTORY_SHA256) {
    throw new Error("INVENTORY_BASELINE_MISMATCH");
  }
  const text = raw.toString("utf8");
  if (!text.endsWith("\r\n")) throw new Error("INVENTORY_TERMINATOR_INVALID");
  const records = text.slice(0, -2).split("\r\n");
  if (records.length !== PRESERVED_INVENTORY_RECORD_COUNT ||
      records.some((line) => line.length === 0)) {
    throw new Error("INVENTORY_RECORD_COUNT_INVALID");
  }
  return hash;
}

function requirePreservedInventory(relativePath, expectedHash) {
  return validatePreservedInventoryBytes(
    readFileSync(fixedExistingFile(relativePath)),
    expectedHash,
  );
}

function git(...args) {
  return execFileSync("git", args, {
    cwd: PROJECT_ROOT,
    encoding: "utf8",
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();
}

export function validateObservedGitAssertions(card, observed) {
  assertExactKeys(
    observed,
    ["changed_paths", "created_at", "head", "parent", "pre_p0_tree", "tree"],
    "OBSERVED_GIT",
  );
  assertStringArray(observed.changed_paths, "OBSERVED_CHANGED_PATHS", {
    allowEmpty: false,
  });
  assertHash(observed.head, 40, "OBSERVED_HEAD");
  assertHash(observed.parent, 40, "OBSERVED_PARENT");
  assertHash(observed.pre_p0_tree, 40, "OBSERVED_PRE_P0_TREE");
  assertHash(observed.tree, 40, "OBSERVED_TREE");
  if (card.code_commit !== observed.head) throw new Error("STALE_CODE_COMMIT");
  if (card.tree_hash !== observed.tree) throw new Error("STALE_TREE_HASH");
  if (observed.parent !== PRE_P0_COMMIT) throw new Error("P0_PARENT_INVALID");
  if (observed.pre_p0_tree !== PRE_P0_TREE) throw new Error("PRE_P0_TREE_CHANGED");
  if (JSON.stringify([...observed.changed_paths].sort()) !==
      JSON.stringify([...card.changed_paths].sort())) {
    throw new Error("CHANGED_PATHS_FORGED");
  }
  if (observed.created_at !== card.created_at) throw new Error("CREATED_AT_STALE");
}

export function verifyRepositoryAssertions(card) {
  validateCardSchema(card);
  if (git("status", "--porcelain=v1", "--untracked-files=all") !== "") {
    throw new Error("WORKTREE_NOT_CLEAN");
  }
  validateObservedGitAssertions(card, {
    changed_paths: git(
      "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD",
    ).split(/\r?\n/).filter(Boolean).sort(),
    created_at: git("show", "-s", "--format=%cI", "HEAD"),
    head: git("rev-parse", "--verify", "HEAD^{commit}"),
    parent: git("rev-parse", "HEAD^1"),
    pre_p0_tree: git("rev-parse", `${PRE_P0_COMMIT}^{tree}`),
    tree: git("rev-parse", "HEAD^{tree}"),
  });
  if (sha256File(`${PATCH_RELATIVE}/09-remediation-plan.md`) !== card.plan_hash) {
    throw new Error("PLAN_HASH_FORGED");
  }
  if (card.approved_plan_hash !== card.plan_hash) {
    throw new Error("APPROVED_PLAN_HASH_FORGED");
  }
  requireMarkdownDecision(
    `${PATCH_RELATIVE}/10-remediation-plan-review.md`, "PLAN_REVIEW",
  );
  for (const key of REVIEW_KEYS) {
    if (sha256File(GOVERNANCE_PATHS[key]) !== card.review_hashes[key]) {
      throw new Error(`REVIEW_HASH_FORGED:${key}`);
    }
  }
  for (const key of ["review_1", "review_2", "review_3"]) {
    requireMarkdownDecision(GOVERNANCE_PATHS[key], key.toUpperCase());
  }
  if (sha256File(`${PATCH_RELATIVE}/15-remediation-synthesis.md`) !==
      card.synthesis_hash) throw new Error("SYNTHESIS_HASH_FORGED");
  requireMarkdownDecision(
    `${PATCH_RELATIVE}/15-remediation-synthesis.md`, "SYNTHESIS",
  );
  for (const key of TEST_KEYS) {
    if (sha256File(TEST_PATHS[key]) !== card.test_output_hashes[key]) {
      throw new Error(`TEST_HASH_FORGED:${key}`);
    }
    if (key !== "rollback") {
      readPassRecord(
        TEST_PATHS[key],
        TRANSCRIPT_PATHS[key],
        FIXED_TEST_COMMAND_BY_KEY[key],
        key.toUpperCase(),
      );
    }
  }
  if (card.environment_hash !== card.test_output_hashes.environment) {
    throw new Error("ENVIRONMENT_HASH_FORGED");
  }
  const before = requirePreservedInventory(
    ".p0-inventory/remediation/remediation-before-evidence-hashes.jsonl",
    card.inventory_hashes.before,
  );
  const after = requirePreservedInventory(
    ".p0-inventory/remediation/remediation-after-evidence-hashes.jsonl",
    card.inventory_hashes.after,
  );
  if (before !== after) throw new Error("INVENTORY_HASH_FORGED_OR_CHANGED");
  requireRollbackProof(card, before);
  const publicPem = readFileSync(fixedExistingFile(PUBLIC_KEY_RELATIVE), "utf8");
  if (publicFingerprint(publicPem) !== ACCEPTED_PUBLIC_KEY_FINGERPRINT) {
    throw new Error("PUBLIC_KEY_FINGERPRINT_UNACCEPTED");
  }
}
