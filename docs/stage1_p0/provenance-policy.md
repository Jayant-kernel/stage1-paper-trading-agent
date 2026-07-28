# Stage 1 baseline provenance policy

This policy is prospective. Evidence produced before the accepted P0-A
baseline is `LEGACY_UNATTESTED`; that label is a reference classification
only and never rewrites, migrates, or recertifies historical files.

## Baseline requirements

A `baseline-manifest-v1` is accepted only for a clean, resolvable Git commit.
It identifies the exact commit, Git tree, committed path set, dependency
locks, exclusion policy, toolchain and secret-scan result. Canonical JSON uses
UTF-8, sorted keys, LF, and no host-local paths.

Committed paths are rejected when they contain:

- `.env` or credential material;
- private signing keys;
- runtime state, logs, caches or generated provenance;
- raw, derived, historical, decision, report or run-card data;
- any `data/evaluation` material, including sealed partitions.

The manifest generator reads committed Git blobs only. It never opens ignored
local `.env` files, runtime evidence or sealed evaluation data.

## Signing boundary

The Ed25519 key under `security/` is public verification material only. The
matching private development-governance key is stored under the exact Windows
Credential Manager target
`Stage1PaperAgent/P0/DevelopmentChangeCard/Ed25519/v1`.

Development signing tools are outside `src/stage1` and are never imported by
the recorder, paper engine, replay, dashboard or session finalizer. This is
not P0-G runtime acceptance-card signing.

## Failure and rollback

Any exclusion, secret, dirty-tree, dependency-lock or reproducibility failure
produces `PROVENANCE_REJECTED`. Rollback uses a new `git revert` commit and
removes only the exact development credential; it never rewrites historical
evidence.
