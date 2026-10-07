"""An empowered investigation stages, serves and explains the toolbox (#8001)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from issue_orchestrator.contracts.improver_toolbox import ImproverMode
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.improver_investigation import (
    EMPOWERED_ADDENDUM,
    EmpoweredInvestigation,
    ScriptedInvestigation,
)
from issue_orchestrator.execution.improver_toolbox_staging import ImproverToolboxStager
from tests.unit.execution.test_improver_toolbox_staging import NOW, _engine

REPO_ROOT = Path(__file__).resolve().parents[3]


def _empowered(addendum: str, github_repos: list[str]) -> EmpoweredInvestigation:
    def github(repo: str) -> None:
        github_repos.append(repo)
        return None

    return EmpoweredInvestigation(
        stager=ImproverToolboxStager(runner=LocalCommandRunner(), clock=lambda: NOW),
        github=github,
        runner=LocalCommandRunner(),
        addendum=addendum,
        budget_minutes=45,
    )


def test_the_toolbox_is_staged_served_and_explained_for_the_agents_run(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    repos: list[str] = []
    investigation = _empowered((REPO_ROOT / EMPOWERED_ADDENDUM).read_text(), repos)

    with investigation.open(engine, run) as kit:
        assert kit.toolbox is not None
        unauthorized = httpx.post(kit.toolbox.url, json={})
        url = kit.toolbox.url

    assert investigation.mode is ImproverMode.EMPOWERED
    assert unauthorized.status_code == 401
    assert (run / "toolbox" / "toolbox.json").is_file()
    assert repos == ["porchpin/porchpin"]
    assert "repos/porchpin/porchpin/issues/N/timeline" in kit.instructions
    assert "about 45 minutes" in kit.instructions and NOW.isoformat() in kit.instructions
    assert "<<" not in kit.instructions
    with pytest.raises(httpx.ConnectError):
        httpx.post(url, json={}, timeout=2)


def test_an_addendum_missing_a_placeholder_is_refused() -> None:
    with pytest.raises(ValueError, match="BUDGET_MINUTES"):
        _empowered("<<AUDITED_REPO>> <<STAGED_AT>>", [])


def test_a_scripted_investigation_has_no_toolbox(tmp_path: Path) -> None:
    with ScriptedInvestigation().open(_engine(tmp_path), tmp_path) as kit:
        assert kit.toolbox is None and kit.instructions == ""
    assert not (tmp_path / "toolbox").exists()
