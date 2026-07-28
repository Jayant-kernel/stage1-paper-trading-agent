from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stage1.security import scan_git_candidate_files  # noqa: E402


def main() -> int:
    findings = scan_git_candidate_files(ROOT)
    if findings:
        print("FAIL  Potential secrets found in Git candidate files:")
        for finding in findings:
            print(f"      {finding.relative_path} ({finding.rule})")
        return 1
    print("PASS  No known credential patterns found in Git candidate files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

