"""Every engine Control Center runs, from its registry and supervisor (#7567)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from issue_orchestrator.adapters.registered_engine_inventory import (
    RegisteredEngineInventory,
    engine_at,
    engine_log_written_at,
)
from issue_orchestrator.contracts.engine_start import EngineStartRecord
from issue_orchestrator.control.tech_lead_charter_policy import TechLeadCharterPolicy
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.engine_start_record import write_engine_start
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


def _start(root: Path, repo: str | None) -> None:
    write_engine_start(
        root / ".issue-orchestrator" / "state",
        EngineStartRecord(
            started_at=NOW - DAY, repo=repo, engine_commit="c" * 40, package_version="0.10.0",
            repo_root=str(root), repo_head=None,
            charter=TechLeadCharterPolicy.from_config(Config()).effective_charter(),
        ),
    )


def _root(tmp_path: Path, name: str, *, started_for: str | None = "") -> Path:
    """A registered root; its engine started for ``owner/<name>`` unless
    ``started_for`` says otherwise (None: no start record at all)."""
    root = (tmp_path / name).resolve()
    (root / ".issue-orchestrator" / "state").mkdir(parents=True)
    if started_for is not None:
        _start(root, started_for or f"owner/{name}")
    return root


def _inventory(registered, supervisor, written: dict[str, datetime] | None = None) -> RegisteredEngineInventory:
    return RegisteredEngineInventory(
        supervisor=supervisor,  # type: ignore[arg-type]
        repositories=lambda: registered,
        last_written=lambda state: (written or {}).get(state.parent.parent.name),
    )


def _running() -> SupervisorStatus:
    return SupervisorStatus(state="running", pid=1, config_name="live.yaml", configuration_mode="codex")


def test_a_running_engine_is_identified_by_its_own_start_record(tmp_path: Path) -> None:
    porchpin = _root(tmp_path, "porchpin")

    read = _inventory([Registered(str(porchpin))], Supervisor({str(porchpin): _running()})).engines(
        since=NOW - DAY
    )

    [sighting] = read.sightings
    assert sighting.running and read.unidentified == ()
    assert sighting.engine.engine_id == configured_repository_key(porchpin)
    assert sighting.engine.repo == "owner/porchpin"
    assert sighting.engine.state_dir == porchpin / ".issue-orchestrator" / "state"


def test_a_reselected_config_never_reattributes_the_state_an_engine_wrote(tmp_path: Path) -> None:
    """r3 F1: the engine started for porchpin/porchpin; the registry now selects
    a config for another repository. Its state is still porchpin's."""
    porchpin = _root(tmp_path, "porchpin", started_for="porchpin/porchpin")

    read = _inventory(
        [Registered(str(porchpin), selected_config="other-repo.yaml")],
        Supervisor({}), {"porchpin": NOW - timedelta(hours=1)},
    ).engines(since=NOW - DAY)

    assert [s.engine.repo for s in read.sightings] == ["porchpin/porchpin"]


def test_an_engine_without_a_start_record_is_unidentified_not_guessed(tmp_path: Path) -> None:
    old = _root(tmp_path, "old", started_for=None)

    read = _inventory([Registered(str(old))], Supervisor({str(old): _running()})).engines(since=NOW - DAY)

    assert read.sightings == ()
    [missing] = read.unidentified
    assert missing.state_dir == old / ".issue-orchestrator" / "state"
    assert "engine-start.json" in missing.reason


def test_recently_running_engines_are_in_scope_and_stale_ones_are_not(tmp_path: Path) -> None:
    io, porchpin, tixmeup = (_root(tmp_path, n) for n in ("io", "porchpin", "tixmeup"))
    crashed = SupervisorStatus(state="failed", pid=9, error="stale lock")
    written = {"porchpin": NOW - timedelta(hours=3), "tixmeup": NOW - timedelta(days=3)}

    read = _inventory(
        [Registered(str(io)), Registered(str(porchpin)), Registered(str(tixmeup))],
        Supervisor({str(porchpin): crashed}), written,
    ).engines(since=NOW - DAY)

    assert [s.engine.repo for s in read.sightings] == ["owner/porchpin"]
    assert not read.sightings[0].running


def test_an_engine_that_stopped_long_ago_is_in_scope_since_an_older_watermark(tmp_path: Path) -> None:
    """r2 F1: in scope by what it wrote since ``since``, however long ago it stopped."""
    porchpin = _root(tmp_path, "porchpin")
    inventory = _inventory([Registered(str(porchpin))], Supervisor({}), {"porchpin": NOW - timedelta(hours=30)})

    assert inventory.engines(since=NOW - DAY).sightings == ()
    assert [s.engine.repo for s in inventory.engines(since=NOW - timedelta(hours=31)).sightings] == [
        "owner/porchpin"
    ]


def test_a_registered_path_that_is_gone_is_skipped(tmp_path: Path) -> None:
    supervisor = Supervisor({})

    read = _inventory([Registered(str(tmp_path / "deleted"))], supervisor).engines(since=NOW - DAY)

    assert read.sightings == () and read.unidentified == ()
    assert supervisor.asked == []


def test_an_engine_is_named_by_its_state_directory(tmp_path: Path) -> None:
    root = _root(tmp_path, "porchpin")

    engine = engine_at(root / ".issue-orchestrator" / "state", "porchpin/porchpin")

    assert engine.engine_id == configured_repository_key(root)
    with pytest.raises(ValueError, match="not an engine state directory"):
        engine_at(tmp_path / "state", "porchpin/porchpin")


def test_a_stopped_named_instance_counts_as_recent_by_its_own_log(tmp_path: Path) -> None:
    """r1 F2: the supervisor captures instance output in orchestrator-<id>.log."""
    state = _root(tmp_path, "porchpin") / ".issue-orchestrator" / "state"
    assert engine_log_written_at(state) is None
    (state / "logs").mkdir()
    old, instance = state / "logs" / "orchestrator.log", state / "logs" / "orchestrator-orchestrator-2.log"
    old.write_text("x")
    instance.write_text("y")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(instance, (2_000_000, 2_000_000))

    assert engine_log_written_at(state) == datetime.fromtimestamp(2_000_000, tz=UTC)
