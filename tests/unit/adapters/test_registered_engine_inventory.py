"""Every engine Control Center runs, from its registry and supervisor (#7567)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from issue_orchestrator.adapters.registered_engine_inventory import (
    RegisteredEngineInventory,
    engine_at,
    engine_log_written_at,
)
from issue_orchestrator.infra.repo_identity import configured_repository_key
from issue_orchestrator.ports.repository_engine_supervisor import MultiInstanceStatus, SupervisorStatus

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
DAY = timedelta(hours=24)


@dataclass
class Registered:
    path: str
    selected_config: str = "main.yaml"
    selected_mode: str = "default"


class Supervisor:
    def __init__(self, running: dict[str, SupervisorStatus]) -> None:
        self.running = running
        self.asked: list[tuple[str, str, str]] = []

    def status_all_instances(self, repo_root, config_name="default.yaml", *, mode="default"):
        self.asked.append((str(repo_root), config_name, mode))
        status = self.running.get(str(repo_root))
        return MultiInstanceStatus(repo_root=str(repo_root), instances=[status] if status else [])


def _root(tmp_path: Path, name: str) -> Path:
    root = (tmp_path / name).resolve()
    (root / ".issue-orchestrator" / "state").mkdir(parents=True)
    return root


def _inventory(registered, supervisor, written: dict[str, datetime] | None = None) -> RegisteredEngineInventory:
    return RegisteredEngineInventory(
        supervisor=supervisor,  # type: ignore[arg-type]
        repositories=lambda: registered,
        repo_slug=lambda root, config, mode: f"owner/{root.name}@{config}:{mode}",
        last_written=lambda state: (written or {}).get(state.parent.parent.name),
    )


def test_a_running_engine_is_in_scope_with_the_config_its_lock_names(tmp_path: Path) -> None:
    porchpin = _root(tmp_path, "porchpin")
    supervisor = Supervisor({str(porchpin): SupervisorStatus(
        state="running", pid=1, config_name="live.yaml", configuration_mode="codex",
    )})

    [engine] = _inventory([Registered(str(porchpin))], supervisor).engines(now=NOW, recent=DAY)

    assert engine.engine_id == configured_repository_key(porchpin)
    assert engine.repo == "owner/porchpin@live.yaml:codex"
    assert engine.state_dir == porchpin / ".issue-orchestrator" / "state"


def test_recently_running_engines_are_in_scope_and_stale_ones_are_not(tmp_path: Path) -> None:
    io, porchpin, tixmeup = (_root(tmp_path, n) for n in ("io", "porchpin", "tixmeup"))
    crashed = SupervisorStatus(state="failed", pid=9, error="stale lock")
    supervisor = Supervisor({str(porchpin): crashed})
    written = {"porchpin": NOW - timedelta(hours=3), "tixmeup": NOW - timedelta(days=3)}

    engines = _inventory(
        [Registered(str(io)), Registered(str(porchpin)), Registered(str(tixmeup))], supervisor, written,
    ).engines(now=NOW, recent=DAY)

    assert [e.repo for e in engines] == ["owner/porchpin@main.yaml:default"]


def test_a_registered_path_that_is_gone_is_skipped(tmp_path: Path) -> None:
    supervisor = Supervisor({})

    assert _inventory([Registered(str(tmp_path / "deleted"))], supervisor).engines(now=NOW, recent=DAY) == ()
    assert supervisor.asked == []


def test_an_engine_is_named_by_its_state_directory(tmp_path: Path) -> None:
    root = _root(tmp_path, "porchpin")

    engine = engine_at(root / ".issue-orchestrator" / "state", "porchpin/porchpin")

    assert engine.engine_id == configured_repository_key(root)
    with pytest.raises(ValueError, match="not an engine state directory"):
        engine_at(tmp_path / "state", "porchpin/porchpin")


def test_a_stopped_named_instance_counts_as_recent_by_its_own_log(tmp_path: Path) -> None:
    """r1 F2: the supervisor captures instance output in orchestrator-<id>.log."""
    import os

    state = _root(tmp_path, "porchpin") / ".issue-orchestrator" / "state"
    assert engine_log_written_at(state) is None
    (state / "logs").mkdir()
    old, instance = state / "logs" / "orchestrator.log", state / "logs" / "orchestrator-orchestrator-2.log"
    old.write_text("x")
    instance.write_text("y")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(instance, (2_000_000, 2_000_000))

    assert engine_log_written_at(state) == datetime.fromtimestamp(2_000_000, tz=UTC)
