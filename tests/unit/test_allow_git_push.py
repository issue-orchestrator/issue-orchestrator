from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "issue_orchestrator" / "templates" / "hooks" / "claude"))

from allow_git_push import is_dry_run_no_verify_push  # noqa: E402


@pytest.mark.parametrize("command", [
    "git push --dry-run --no-verify origin branch",
    "git push --no-verify --dry-run origin branch",
    "git push origin branch --dry-run --no-verify",
])
def test_allows_dry_run_no_verify_push(command: str) -> None:
    assert is_dry_run_no_verify_push(command) is True


@pytest.mark.parametrize("command", [
    "git push --dry-run origin branch",
    "git push --no-verify origin branch",
    "git commit --no-verify -m test",
    "gh pr merge 123",
])
def test_rejects_other_commands(command: str) -> None:
    assert is_dry_run_no_verify_push(command) is False


def test_rejects_invalid_shell() -> None:
    assert is_dry_run_no_verify_push("git push \"unterminated") is False
