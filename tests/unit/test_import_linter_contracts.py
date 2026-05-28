from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]


def _import_linter_contracts() -> list[dict[str, Any]]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["importlinter"]["contracts"]


def test_control_observation_adapter_boundary_contract_is_enabled() -> None:
    contracts = {contract["name"]: contract for contract in _import_linter_contracts()}
    contract = contracts["Control and observation must not import adapters directly"]

    assert contract["type"] == "forbidden"
    assert contract["source_modules"] == [
        "issue_orchestrator.control",
        "issue_orchestrator.observation",
    ]
    assert contract["forbidden_modules"] == ["issue_orchestrator.adapters"]
    assert contract["allow_indirect_imports"] is True
    assert contract["ignore_imports"] == [
        "issue_orchestrator.control.session_completion_diagnostics -> "
        "issue_orchestrator.adapters.session_log.registry",
    ]
