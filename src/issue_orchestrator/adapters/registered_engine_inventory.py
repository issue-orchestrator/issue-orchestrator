"""The engines Control Center runs, from its registry and its supervisor (#7567).

Every repository the Control Center registry holds is a candidate; its engine
is in scope when the supervisor reports one running (a live lock), or when
its engine log was written at or after ``since`` (it stopped or crashed after
that).
Everything here is read only: the registry file, each repository's lock files
and config, and one ``stat`` of its log. Nothing in a target repository is
written, and no database is opened.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from ..domain.engine_activity import EngineRef, EngineSighting, ran_since
from ..infra.repo_identity import configured_repository_key, normalize_repo_root, state_dir
from ..ports.repository_engine_supervisor import RUNNING_SUPERVISOR_STATE, SupervisorOps
from .configured_repository_registry import (
    RegisteredRepository,
    configured_repo_slug,
    registered_repositories,
)

#: The engine's logs, relative to its state directory: the engine's own
#: logger writes ``orchestrator.log``; the supervisor captures a named
#: instance's output in ``orchestrator-<instance>.log``.
ENGINE_LOGS = "logs/orchestrator*.log"

RepoSlug = Callable[[Path, str, str], str]
LastWritten = Callable[[Path], datetime | None]


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
        repo_slug: RepoSlug = configured_repo_slug,
        last_written: LastWritten = engine_log_written_at,
    ) -> None:
        self._supervisor = supervisor
        self._repositories = repositories
        self._repo_slug = repo_slug
        self._last_written = last_written

    def engines(self, *, since: datetime) -> tuple[EngineSighting, ...]:
        engines: dict[str, EngineSighting] = {}
        for registered in self._repositories():
            root = normalize_repo_root(registered.path)
            if not root.is_dir():
                continue
            sighting = self._sighting(registered, root, since=since)
            if sighting is not None:
                engines.setdefault(sighting.engine.engine_id, sighting)
        return tuple(engines.values())

    def _sighting(
        self, registered: RegisteredRepository, root: Path, *, since: datetime
    ) -> EngineSighting | None:
        """The engine, with the config it runs (a live lock's) or the selected
        one; None when it neither runs nor wrote its log since ``since``."""
        statuses = self._supervisor.status_all_instances(
            root, config_name=registered.selected_config, mode=registered.selected_mode
        )
        running = sorted(
            (s for s in statuses.instances if s.state == RUNNING_SUPERVISOR_STATE),
            key=lambda s: s.instance_id or "",
        )
        written = self._last_written(state_dir(root))
        if not ran_since(running=bool(running), last_written=written, since=since):
            return None
        # The slug is read only for an engine in scope: a long-stopped
        # registration's config may be gone.
        config_name, mode = (
            (running[0].config_name, running[0].configuration_mode)
            if running
            else (registered.selected_config, registered.selected_mode)
        )
        return EngineSighting(
            engine=EngineRef(
                engine_id=configured_repository_key(str(root)),
                repo=self._repo_slug(root, config_name, mode),
                state_dir=state_dir(root),
            ),
            running=bool(running),
            last_written=written,
        )


__all__ = ["ENGINE_LOGS", "RegisteredEngineInventory", "engine_at", "engine_log_written_at"]
