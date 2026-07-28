from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stage1.config import load_config  # noqa: E402
from stage1.evaluation.intraday_lab import (  # noqa: E402
    load_intraday_protocol,
    run_intraday_lab,
    write_intraday_lab_artifact,
)
from stage1.paper.costs import load_cost_table  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare frozen intraday paper variants on saved sessions."
    )
    parser.add_argument(
        "--protocol",
        default="config/intraday_variants.yaml",
        help="Frozen protocol path inside the project",
    )
    args = parser.parse_args()
    protocol_path = (ROOT / args.protocol).resolve()
    if not protocol_path.is_relative_to(ROOT.resolve()) or not protocol_path.is_file():
        print(json.dumps({"status": "error", "reason": "invalid protocol path"}))
        return 2
    stage1 = load_config(ROOT / "config" / "stage1.yaml")
    protocol = load_intraday_protocol(protocol_path, stage1=stage1)
    report = run_intraday_lab(
        project_root=ROOT,
        stage1=stage1,
        protocol=protocol,
        cost_table=load_cost_table(ROOT / "config" / "cost_profiles.yaml"),
        protocol_sha256=hashlib.sha256(protocol_path.read_bytes()).hexdigest(),
    )
    artifact = write_intraday_lab_artifact(project_root=ROOT, report=report)
    print(
        json.dumps(
            {
                "status": "ok",
                "artifact": asdict(artifact),
                "gate": report["gate"],
                "development_ranking_zero_delay": report[
                    "development_ranking_zero_delay"
                ],
                "diagnostics": report["diagnostics"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
