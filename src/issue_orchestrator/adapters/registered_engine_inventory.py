"""The engines Control Center runs, from its registry and its supervisor (#7567).

Every repository the Control Center registry holds is a candidate; its engine
is in scope when the supervisor reports one running (a live lock), or when it
wrote a log at or after ``since`` (it stopped or crashed after that).

An engine is identified by its own start record (``engine-start.json``): the
repository it works is what its config said when it STARTED, so a config
edited or re-selected since never re-attributes the state it wrote. An engine
in scope without a readable record (it started on an older version) is
reported unidentified, never guessed at.

Everything here is read only: the registry file, each repository's lock
files, its start record and one ``stat`` per log. Nothing in a target
repository is written, and no database is opened.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from ..contracts.engine_start import EngineStartRecord
from ..domain.engine_activity import (
    EngineInventoryRead,
    EngineRef,
    EngineSighting,
    UnidentifiedEngine,
    ran_since,
)
from ..infra.engine_start_record import EngineStartRecordUnavailable, read_engine_start
from ..infra.repo_identity import configured_repository_key, normalize_repo_root, state_dir
from ..ports.repository_engine_supervisor import RUNNING_SUPERVISOR_STATE, SupervisorOps
from .configured_repository_registry import RegisteredRepository, registered_repositories

#: The engine's logs, relative to its state directory: the engine's own
#: logger writes ``orchestrator.log``; the supervisor captures a named
#: instance's output in ``orchestrator-<instance>.log``.
ENGINE_LOGS = "logs/orchestrator*.log"

LastWritten = Callable[[Path], datetime | None]
StartRecord = Callable[[Path], EngineStartRecord]


def engine_log_written_at(state: Path) -> datetime | None:
    """When the engine last wrote any of its logs, or None if it has none."""
    written = []
    for log in state.glob(ENGINE_LOGS):
        try:
            written.append(log.stat().st_mtime)
        except FileNotFoundError:  # rotated away between the glob and the stat
            continue
    return datetime.fromtimestamp(max(written), tz=UTC) if written else None


def engine_at(state: Path, repo: str) -> EngineRef:
    """The engine whose state directory is ``state`` (``<root>/.issue-orchestrator/state``)."""
    root = normalize_repo_root(state).parent.parent
    if state_dir(root) != normalize_repo_root(state):
        raise ValueError(f"{state} is not an engine state directory (<repo>/.issue-orchestrator/state)")
    return EngineRef(engine_id=configured_repository_key(root), repo=repo, state_dir=state_dir(root))


class RegisteredEngineInventory:
    def __init__(
        self,
        *,
        supervisor: SupervisorOps,
        repositories: Callable[[], Sequence[RegisteredRepository]] = registered_repositories,
        start_record: StartRecord = read_engine_start,
        last_written: LastWritten = engine_log_written_at,
    ) -> None:
        self._supervisor = supervisor
        self._repositories = repositories
        self._start_record = start_record
        self._last_written = last_written

    def engines(self, *, since: datetime) -> EngineInventoryRead:
        sightings: list[EngineSighting] = []
        unidentified: list[UnidentifiedEngine] = []
        seen: set[Path] = set()
        for registered in self._repositories():
            root = normalize_repo_root(registered.path)
            if not root.is_dir() or root in seen:
                continue
            seen.add(root)
            running = self._running(registered, root)
            written = self._last_written(state_dir(root))
            if not ran_since(running=running, last_written=written, since=since):
                continue
            identity = self._identify(root)
            if isinstance(identity, UnidentifiedEngine):
                unidentified.append(identity)
                continue
            sightings.append(EngineSighting(identity, running=running, last_written=written))
        return EngineInventoryRead(sightings=tuple(sightings), unidentified=tuple(unidentified))

    def _running(self, registered: RegisteredRepository, root: Path) -> bool:
        statuses = self._supervisor.status_all_instances(
            root, config_name=registered.selected_config, mode=registered.selected_mode
        )
        return any(s.state == RUNNING_SUPERVISOR_STATE for s in statuses.instances)

    def _identify(self, root: Path) -> EngineRef | UnidentifiedEngine:
        state = state_dir(root)
        try:
            record = self._start_record(state)
        except EngineStartRecordUnavailable as error:
            return UnidentifiedEngine(state, f"{error}")
        if record.repo is None:
            return UnidentifiedEngine(state, "its start record names no repository")
        if normalize_repo_root(record.repo_root) != root:
            return UnidentifiedEngine(
                state, f"its start record belongs to {record.repo_root}, not {root}"
            )
        return EngineRef(
            engine_id=configured_repository_key(root), repo=record.repo, state_dir=state
        )


__all__ = ["ENGINE_LOGS", "RegisteredEngineInventory", "engine_at", "engine_log_written_at"]
