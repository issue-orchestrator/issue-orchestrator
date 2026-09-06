"""Publication/safety diff facts must not depend on Git presentation settings."""

from pathlib import Path

import pytest

from issue_orchestrator.adapters.git.git_cli import GitCLI
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.git_working_copy import GitWorkingCopy


def git(repo: Path, *args: str) -> str:
    return GitCLI(runner=LocalCommandRunner()).run(repo, list(args)).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test User")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / ".gitattributes").write_text("*.txt diff=opaque\n")
    (tmp_path / "a.txt").write_text("original\n")
    git(tmp_path, "add", ".gitattributes", "a.txt")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


@pytest.mark.parametrize("changed", [False, True])
def test_publication_diff_ignores_text_conversion(repo: Path, changed: bool) -> None:
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "config", "diff.opaque.textconv", "true")
    if changed:
        (repo / "a.txt").write_text("valuable change\n")
        git(repo, "add", "a.txt")
        git(repo, "commit", "-q", "-m", "change")
    # Human-facing diff presentation hides this change completely.
    assert git(repo, "diff", f"{base}...HEAD") == ""

    result = GitWorkingCopy().diff_against_base(repo, base)

    assert result.success
    assert bool(result.diff_text) is changed
    if changed:
        assert "+valuable change" in result.diff_text


def test_publication_diff_does_not_hide_gitlink_changes(repo: Path) -> None:
    first = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "-q", "--allow-empty", "-m", "second target")
    second = git(repo, "rev-parse", "HEAD")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{first},module")
    git(repo, "commit", "-q", "-m", "base gitlink")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "update-index", "--cacheinfo", f"160000,{second},module")
    git(repo, "commit", "-q", "-m", "change gitlink")
    git(repo, "config", "diff.ignoreSubmodules", "all")
    assert git(repo, "diff", f"{base}...HEAD") == ""

    result = GitWorkingCopy().diff_against_base(repo, base)

    assert result.success
    assert "module" in result.diff_text
    assert first in result.diff_text and second in result.diff_text
