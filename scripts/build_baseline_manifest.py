from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stage1.validation.provenance import (  # noqa: E402
    ProvenanceError,
    build_baseline_manifest,
    write_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build baseline-manifest-v1")
    parser.add_argument("--approved-by", default="Jayant Saxena")
    parser.add_argument(
        "--confirm-sealed-tests-pass",
        choices=("YES",),
        required=True,
        help="Required reviewed attestation; no false or omitted mode exists.",
    )
    arguments = parser.parse_args()
    try:
        manifest = build_baseline_manifest(
            ROOT,
            approved_by=arguments.approved_by,
            sealed_test_exclusion_attested=True,
        )
        output = write_manifest(ROOT, manifest)
    except ProvenanceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "manifest_hash": manifest["manifest_hash"],
                "relative_path": output.relative_to(ROOT).as_posix(),
                "status": "BASELINED",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
