"""Tests for client-host local path integrations."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from issue_orchestrator.execution.client_host import DarwinClientHost


def test_darwin_open_path_wraps_subprocess_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    host = DarwinClientHost()

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(returncode=1, cmd=["open", "/tmp/prompt.txt"])

    monkeypatch.setattr(subprocess, "run", fail_run)

    with pytest.raises(RuntimeError, match="Failed to open path: /tmp/prompt.txt"):
        host.open_path(Path("/tmp/prompt.txt"))


def test_darwin_reveal_worktree_wraps_subprocess_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    host = DarwinClientHost()

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(returncode=1, cmd=["open", "/tmp/worktree"])

    monkeypatch.setattr(subprocess, "run", fail_run)

    with pytest.raises(RuntimeError, match="Failed to open worktree: /tmp/worktree"):
        host.reveal_worktree(Path("/tmp/worktree"))
