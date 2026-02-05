"""Shared fixtures for integration tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Generator

import pytest


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
