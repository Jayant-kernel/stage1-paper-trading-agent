from pathlib import Path

from stage1.security import scan_git_candidate_files

ROOT = Path(__file__).resolve().parents[1]


def test_git_candidate_files_contain_no_known_secret_patterns() -> None:
    assert scan_git_candidate_files(ROOT) == []

