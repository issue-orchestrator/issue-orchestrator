"""Tests for the git stack-branch ancestry checker (ADR-0029, #6596).

These verify the production checker decides successor↔predecessor containment
from real ``git`` exit codes through a CommandRunner, and that every failure
path is fail-safe (a stale answer of ``False``) so a possibly-stale successor is
never published or merged on an unverified base.
"""

from pathlib import Path

from issue_orchestrator.execution.stack_branch_ancestry import GitStackBranchAncestry
from issue_orchestrator.ports.command_runner import CommandResult


class FakeCommandRunner:
    """Records git invocations and replays canned results per command verb."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.fetch_rc = 0
        self.ancestor_rc = 0
        self.raise_on_ancestor = False

    def run(self, command, cwd=None, env=None, timeout_seconds=None, shell=False):
        self.calls.append(list(command))
        if command[:2] == ["git", "fetch"]:
            return CommandResult(returncode=self.fetch_rc, stdout="", stderr="boom")
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            if self.raise_on_ancestor:
                raise RuntimeError("git exploded")
            return CommandResult(returncode=self.ancestor_rc, stdout="", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")


def _checker(runner):
    return GitStackBranchAncestry(runner)


def test_contained_when_ancestor_exit_zero():
    runner = FakeCommandRunner()
    runner.fetch_rc = 0
    runner.ancestor_rc = 0  # FETCH_HEAD is an ancestor of HEAD

    assert _checker(runner).successor_contains_predecessor(Path("/wt"), "20-base") is True
    # It fetched the predecessor branch then asked merge-base.
    assert ["git", "fetch", "origin", "20-base"] in runner.calls
    assert ["git", "merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD"] in runner.calls


def test_stale_when_ancestor_exit_one():
    runner = FakeCommandRunner()
    runner.fetch_rc = 0
    runner.ancestor_rc = 1  # predecessor advanced past the successor

    assert _checker(runner).successor_contains_predecessor(Path("/wt"), "20-base") is False


def test_fetch_failure_is_fail_safe():
    runner = FakeCommandRunner()
    runner.fetch_rc = 128  # could not fetch the predecessor branch

    assert _checker(runner).successor_contains_predecessor(Path("/wt"), "20-base") is False
    # Never reaches the ancestry check when the fetch failed.
    assert not any(c[:3] == ["git", "merge-base", "--is-ancestor"] for c in runner.calls)


def test_unexpected_exception_is_fail_safe():
    runner = FakeCommandRunner()
    runner.raise_on_ancestor = True

    assert _checker(runner).successor_contains_predecessor(Path("/wt"), "20-base") is False


def test_empty_branch_is_fail_safe_without_running_git():
    runner = FakeCommandRunner()

    assert _checker(runner).successor_contains_predecessor(Path("/wt"), "") is False
    assert runner.calls == []
