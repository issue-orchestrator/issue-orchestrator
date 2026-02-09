"""Unit tests for command-backed Control Center actions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.execution.control_center_actions import (
    ListStaleWorktreesCommand,
    PauseOrchestratorCommand,
    RefreshActionRequest,
    RefreshOrchestratorCommand,
    RepoActionRequest,
    TraceActionRequest,
    TraceIssueCommand,
)
from issue_orchestrator.infra.supervisor import SupervisorStatus


@pytest.mark.asyncio
async def test_pause_command_returns_not_running() -> None:
    supervisor = MagicMock()
    supervisor.status.return_value = SupervisorStatus(state="stopped")
    cmd = PauseOrchestratorCommand(supervisor)

    result = await cmd.execute(RepoActionRequest(repo_root=Path("/tmp/repo")))

    assert result.status_code == 400
    assert result.payload["error"] == "not_running"
    assert result.payload["state"] == "stopped"


@pytest.mark.asyncio
async def test_trace_command_scopes_to_last_start(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    log_dir = repo_root / ".issue-orchestrator" / "state" / "logs"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "orchestrator.log"
    log_file.write_text(
        "\n".join([
            "old issue=4070 before startup",
            "Starting orchestrator",
            "tick issue=4070 in current run",
            "noise line",
        ]),
    )
    cmd = TraceIssueCommand()

    result = await cmd.execute(TraceActionRequest(repo_root=repo_root, issue_number=4070))

    assert result.status_code == 200
    assert result.payload["entries"] == ["tick issue=4070 in current run"]
    assert result.payload["total"] == 1


@pytest.mark.asyncio
async def test_pause_command_uses_async_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = MagicMock()
    supervisor.status.return_value = SupervisorStatus(state="running", port=18080)
    cmd = PauseOrchestratorCommand(supervisor)

    calls: dict[str, bool] = {"pause": False, "close": False}

    class FakeAsyncApi:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        async def pause(self) -> dict[str, str]:
            calls["pause"] = True
            return {"status": "paused"}

        async def close(self) -> None:
            calls["close"] = True

    monkeypatch.setattr(
        "issue_orchestrator.execution.control_center_actions.OrchestratorAsyncHttpApi",
        FakeAsyncApi,
    )

    result = await cmd.execute(RepoActionRequest(repo_root=Path("/tmp/repo")))

    assert result.status_code == 200
    assert result.payload == {"status": "paused"}
    assert calls == {"pause": True, "close": True}


@pytest.mark.asyncio
async def test_refresh_command_forwards_inflight_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = MagicMock()
    supervisor.status.return_value = SupervisorStatus(state="running", port=18080)
    cmd = RefreshOrchestratorCommand(supervisor)

    captured: dict[str, list[str] | None] = {"ids": None}

    class FakeAsyncApi:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        async def refresh(self, inflight_stable_ids: list[str]) -> dict[str, str]:
            captured["ids"] = inflight_stable_ids
            return {"status": "refresh_requested"}

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        "issue_orchestrator.execution.control_center_actions.OrchestratorAsyncHttpApi",
        FakeAsyncApi,
    )

    result = await cmd.execute(
        RefreshActionRequest(repo_root=Path("/tmp/repo"), inflight_stable_ids=["I_1", "I_2"]),
    )

    assert result.status_code == 200
    assert result.payload == {"status": "refresh_requested"}
    assert captured["ids"] == ["I_1", "I_2"]


@pytest.mark.asyncio
async def test_stale_worktrees_fallback_without_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "trustlist"
    repo_root.mkdir()
    stale = tmp_path / "trustlist-4070"
    stale.mkdir()
    (stale / ".git").write_text("gitdir: /tmp/fake")

    # Force fallback mode (no config available).
    monkeypatch.setattr(
        "issue_orchestrator.infra.config.Config.find_and_load",
        staticmethod(lambda start_path: (_ for _ in ()).throw(FileNotFoundError())),
    )

    fake_git = SimpleNamespace(list_active_worktrees=lambda _repo: set())
    monkeypatch.setattr(
        "issue_orchestrator.execution.git_working_copy.GitWorkingCopy",
        lambda: fake_git,
    )

    cmd = ListStaleWorktreesCommand()
    result = await cmd.execute(RepoActionRequest(repo_root=repo_root))

    assert result.status_code == 200
    assert result.payload["scope"] == "repo-parent-fallback"
    paths = [entry["path"] for entry in result.payload["stale_worktrees"]]
    assert str(stale) in paths
