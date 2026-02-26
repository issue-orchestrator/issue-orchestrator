"""Shared fixtures for integration tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Generator

import pytest

_BASE_REPO_ROOT = Path(__file__).resolve().parents[2]
_STATE_DIR = _BASE_REPO_ROOT / ".issue-orchestrator" / "state"


@pytest.fixture(autouse=True)
def _assert_no_base_repo_state_pollution():
    """Guardrail: integration tests must never modify SQLite files in base repo.

    Snapshots the base repo's .issue-orchestrator/state/ directory before each
    test and asserts:
    1. No new .sqlite files appeared (creation pollution)
    2. No existing .sqlite files were deleted (deletion culprit detection)

    This catches any test or code path that accidentally targets the real repo
    root instead of tmp_path.
    """
    before = set(_STATE_DIR.glob("*.sqlite*")) if _STATE_DIR.exists() else set()
    yield
    after = set(_STATE_DIR.glob("*.sqlite*")) if _STATE_DIR.exists() else set()
    new_files = after - before
    assert not new_files, (
        f"Integration test created SQLite file(s) in base repo state dir!\n"
        f"New files: {new_files}\n"
        f"State dir: {_STATE_DIR}\n"
        f"Tests must use tmp_path for state, not the real repo root."
    )
    deleted_files = before - after
    assert not deleted_files, (
        f"Integration test DELETED SQLite file(s) from base repo state dir!\n"
        f"Deleted files: {deleted_files}\n"
        f"State dir: {_STATE_DIR}\n"
        f"This is the likely culprit for the mystery state file deletion."
    )


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path: Path) -> Generator[Path, None, None]:
    """Isolate all integration tests from the production registry.

    Sets ISSUE_ORCHESTRATOR_CONFIG_DIR to a temp directory so tests
    don't pollute the user's real registry at ~/.config/issue-orchestrator/.

    This is critical for tests that:
    - Start control center as a subprocess (env var is inherited)
    - Register repos via API
    - Use the repo registry directly

    Also sets ISSUE_ORCHESTRATOR_SKIP_DOCTOR to skip slow health checks.
    """
    config_dir = tmp_path / "test-config"
    config_dir.mkdir()

    old_config = os.environ.get("ISSUE_ORCHESTRATOR_CONFIG_DIR")
    old_skip = os.environ.get("ISSUE_ORCHESTRATOR_SKIP_DOCTOR")

    os.environ["ISSUE_ORCHESTRATOR_CONFIG_DIR"] = str(config_dir)
    os.environ["ISSUE_ORCHESTRATOR_SKIP_DOCTOR"] = "1"

    yield config_dir

    # Restore original values
    if old_config is None:
        os.environ.pop("ISSUE_ORCHESTRATOR_CONFIG_DIR", None)
    else:
        os.environ["ISSUE_ORCHESTRATOR_CONFIG_DIR"] = old_config

    if old_skip is None:
        os.environ.pop("ISSUE_ORCHESTRATOR_SKIP_DOCTOR", None)
    else:
        os.environ["ISSUE_ORCHESTRATOR_SKIP_DOCTOR"] = old_skip


@pytest.fixture(autouse=True)
def _strip_nested_session_env(monkeypatch):
    """Allow Claude subprocess invocations from within a Claude Code session.

    Claude Code sets CLAUDECODE and CLAUDE_CODE_ENTRYPOINT to detect nested
    launches. Strip them so integration tests that spawn Claude subprocesses
    work regardless of whether the test runner itself is a Claude Code agent.
    """
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
