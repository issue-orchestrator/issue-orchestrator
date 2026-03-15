from __future__ import annotations

import json
from pathlib import Path

from issue_orchestrator.infra.config import Config


def test_codespaces_config_loads_with_stable_web_ports() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config_path = repo_root / ".issue-orchestrator" / "config" / "z-codespaces.yaml"

    config = Config.load(config_path)

    assert config.web_port == 8080
    assert config.control_api_port == 19081
    assert config.terminal_adapter == "subprocess"


def test_devcontainer_forwards_codespaces_ports_and_bootstraps_repo() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    devcontainer_path = repo_root / ".devcontainer" / "devcontainer.json"

    data = json.loads(devcontainer_path.read_text(encoding="utf-8"))

    assert data["postCreateCommand"] == "make worktree-setup && npm install -g @openai/codex"
    assert data["forwardPorts"] == [19080, 19081, 8080]
    assert data["portsAttributes"]["19080"]["label"] == "Issue Orchestrator Control Center"
    assert data["portsAttributes"]["8080"]["label"] == "Issue Orchestrator Engine Dashboard"
