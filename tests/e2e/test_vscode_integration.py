"""E2E test that validates the VS Code extension against a live orchestrator."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)


@pytest.mark.e2e
@pytest.mark.gh_activity_limit(test_gh_activity_limit=1, system_gh_activity_limit=5)
def test_vscode_extension_e2e(
    e2e_orchestrator,
    e2e_project_root: Path,
    e2e_session_config,
) -> None:
    node_modules = e2e_project_root / "packages" / "vscode" / "node_modules"
    if not node_modules.exists():
        pytest.fail("Missing packages/vscode/node_modules. Run: make install-vscode-extensions")

    config_path = e2e_orchestrator.config_path()
    env = os.environ.copy()
    # Ensure the Python venv bin is on PATH so issue-orchestrator-mcp is found
    venv_bin = e2e_project_root / ".venv" / "bin"
    if venv_bin.exists():
        env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
    env["IO_VSCODE_E2E"] = "1"
    env["IO_E2E_CONFIG_PATH"] = str(config_path)
    env["IO_E2E_REPO_ROOT"] = str(e2e_project_root)
    env["IO_E2E_REPO_NAME"] = e2e_session_config.repo
    env["IO_E2E_API_PORT"] = str(e2e_session_config.web_port)
    env["IO_VSCODE_TEST_WORKSPACE"] = str(e2e_project_root)

    logger.info("Running VS Code extension e2e test with config=%s", config_path)
    result = subprocess.run(
        ["make", "test-vscode"],
        cwd=e2e_project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        logger.error("VS Code extension e2e failed (stdout):\n%s", result.stdout)
        logger.error("VS Code extension e2e failed (stderr):\n%s", result.stderr)
        pytest.fail(f"VS Code extension e2e failed with code {result.returncode}")
