"""Exporting the engine's source at its exact commit (#7490)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.execution.engine_source_archive import (
    EngineSourceUnavailable,
    GitEngineSourceArchive,
)
from issue_orchestrator.execution.command_runner import LocalCommandRunner


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "io"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "engine.py").write_text("v1\n")
    _git(repo, "add", "engine.py")
    _git(repo, "commit", "-q", "-m", "one")
    return repo


def test_the_tree_is_the_commits_not_the_checkouts(repo: Path, tmp_path: Path) -> None:
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "engine.py").write_text("v2\n")
    _git(repo, "commit", "-q", "-am", "two")
    (repo / "engine.py").write_text("uncommitted\n")

    GitEngineSourceArchive(repo, LocalCommandRunner()).export(first, tmp_path / "out")

    assert (tmp_path / "out" / "engine.py").read_text() == "v1\n"


def test_a_commit_the_repository_lacks_is_unavailable(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(EngineSourceUnavailable, match="fetch it"):
        GitEngineSourceArchive(repo, LocalCommandRunner()).export("f" * 40, tmp_path / "out")
    assert not (tmp_path / "out").exists()
