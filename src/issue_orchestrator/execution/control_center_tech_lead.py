"""The Control Center's cross-repository Tech lead page (#7763).

The Control Center owns no tech-lead state. Each configured repository's
running Repository Engine projects its own section (``GET /api/tech-lead/page``)
from what it already holds, and owns Approve/Decline
(``POST /api/tech-lead/proposals``). This owner only fans out across the
configured repositories, validates each engine's answer against the generated
contract (fail closed: an engine that answers off-contract is shown as
unavailable, never trusted), and proxies one typed command to the one engine
that owns the proposal's repository.

A repository whose engine is not running is a row that says so, not an error:
the page still shows every other repository, and approving on GitHub keeps
working without any engine.

A running engine that misses one read is shown as what it last reported, as of
when (``last_known``), or as unknown when nothing was ever read (#8988). Either
way the page's total is then not a number: a count the page cannot vouch for is
never shown as one, least of all as 0. What an engine last reported belongs to
that engine's life: page reads run concurrently, so a read that lands after its
engine stopped, restarted or was read again more recently is never remembered.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol, Sequence

from pydantic import ValidationError

from ..contracts.ui_openapi_models import (
    ControlCenterTechLeadAvailableRepoPayload,
    ControlCenterTechLeadLastKnownRepoPayload,
    ControlCenterTechLeadPayload,
    ControlCenterTechLeadRepoPayload,
    ControlCenterTechLeadUnreportedRepoPayload,
    TechLeadPageSectionPayload,
    TechLeadProposalCommandPayload,
    TechLeadProposalOutcomePayload,
)
from ..infra.repo_identity import configured_repository_key
from .engine_command_failure import ENGINE_COMMAND_TIMEOUT_SECONDS, EngineCommandFailure, off_contract_answer_failure
from .orchestrator_http_api import post_orchestrator_json, probe_orchestrator_json

if TYPE_CHECKING:
    from ..infra.repo_registry import RegisteredRepo
    from ..ports.repository_engine_supervisor import SupervisorOps, SupervisorStatus

logger = logging.getLogger(__name__)

#: Per-engine read budget: one slow engine must not stall the page.
SECTION_TIMEOUT_SECONDS = 3.0
#: The engine decides a proposal under its state lock, which a running tick
#: holds for the whole tick — the shared engine-command budget covers that wait.
COMMAND_TIMEOUT_SECONDS = ENGINE_COMMAND_TIMEOUT_SECONDS


class EngineTechLeadTransport(Protocol):
    """How the Control Center reaches one engine's Tech lead surface."""

    def read_section(self, port: int) -> dict[str, Any] | None:
        """The engine's section JSON, or ``None`` if it could not be read."""
        ...

    def send_command(
        self, port: int, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]] | EngineCommandFailure:
        """``(HTTP status, JSON object)`` from the engine, or why it failed."""
        ...


_DECISION_COMMAND = "tech-lead proposal decision"


def _proposals_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/api/tech-lead/proposals"


class HttpEngineTechLeadTransport:
    """The production transport: the engine's authenticated loopback API."""

    def read_section(self, port: int) -> dict[str, Any] | None:
        return probe_orchestrator_json(
            f"http://127.0.0.1:{port}/api/tech-lead/page",
            timeout_seconds=SECTION_TIMEOUT_SECONDS,
        )

    def send_command(
        self, port: int, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]] | EngineCommandFailure:
        return post_orchestrator_json(
            _proposals_url(port),
            body,
            command=_DECISION_COMMAND,
            timeout_seconds=COMMAND_TIMEOUT_SECONDS,
        )


@dataclass(frozen=True)
class RunningEngine:
    """One life of a repository's running engine, as the supervisor saw it.

    A restarted engine is another life, even on the same port: its process,
    start time and instance tell them apart (#8988).
    """

    port: int
    pid: int | None
    started_at: str | None
    instance_id: str | None

    @classmethod
    def observed(cls, status: "SupervisorStatus") -> "RunningEngine | None":
        if status.state != "running" or not status.port:
            return None
        return cls(port=status.port, pid=status.pid, started_at=status.started_at, instance_id=status.instance_id)


@dataclass(frozen=True)
class SectionRead:
    """One page read of one engine life's section, issued before its I/O."""

    repo_key: str
    engine: RunningEngine
    generation: int


@dataclass
class _EngineLifeMemory:
    """What one engine life reported, from the first read that observed it."""

    engine: RunningEngine
    since: int
    section: TechLeadPageSectionPayload | None = None
    section_read: int = 0


class TechLeadSectionMemory:
    """Each repository's last section its running engine reported (#8988).

    Lives as long as the Control Center process, across page reads, so one
    missed read shows what the engine last reported instead of nothing.

    Page reads run concurrently on the route's thread pool and their engine
    reads land in any order, so every read takes a :class:`SectionRead` before
    its I/O and the memory decides, atomically, what its answer may change:
    only a read of the current engine life commits, and only if no newer read
    has already committed. An engine seen stopped, or seen as another life,
    voids its earlier life's report and every read of it still in flight.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()  # page reads run on the route's thread pool
        self._generation = 0
        self._lives: dict[str, _EngineLifeMemory] = {}

    def engine_stopped(self, repo_key: str) -> None:
        """No engine runs for *repo_key*: nothing it reported outlives it."""
        with self._lock:
            self._generation += 1
            self._lives.pop(repo_key, None)

    def begin_read(self, repo_key: str, engine: RunningEngine) -> SectionRead:
        """Observe *engine* running and take this read's place in the order."""
        with self._lock:
            self._generation += 1
            life = self._lives.get(repo_key)
            if life is None or life.engine != engine:
                self._lives[repo_key] = _EngineLifeMemory(engine=engine, since=self._generation)
            return SectionRead(repo_key, engine, self._generation)

    def answered(self, read: SectionRead, section: TechLeadPageSectionPayload) -> None:
        """*read*'s engine answered: remember it, unless the read is stale."""
        with self._lock:
            life = self._current_life(read)
            if life is not None and read.generation > life.section_read:
                life.section, life.section_read = section, read.generation

    def last_report(self, read: SectionRead) -> TechLeadPageSectionPayload | None:
        """What *read*'s engine life last reported, if that life is current."""
        with self._lock:
            life = self._current_life(read)
            return life.section if life is not None else None

    def _current_life(self, read: SectionRead) -> _EngineLifeMemory | None:
        life = self._lives.get(read.repo_key)
        # A life observed after this read began replaced the read's own.
        return life if life is not None and life.since <= read.generation else None


#: A running engine whose backlog this read did not learn.
_RUNNING_UNREPORTED = frozenset({"last_known", "unavailable"})
_NO_ANSWER = "The engine is running but did not answer"
_OFF_CONTRACT = "The engine's answer was not understood (version mismatch?)"


@dataclass(frozen=True)
class TechLeadCommandResult:
    status_code: int
    outcome: TechLeadProposalOutcomePayload


class UnknownTechLeadRepositoryError(LookupError):
    """No configured repository has the requested key."""


@dataclass
class ControlCenterTechLead:
    supervisor: "SupervisorOps"
    list_repos: Callable[[], Sequence["RegisteredRepo"]]
    transport: EngineTechLeadTransport
    #: The Control Center's one memory: the owner itself is built per request.
    memory: TechLeadSectionMemory
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def page(self) -> ControlCenterTechLeadPayload:
        rows = [self._repo_row(repo) for repo in self.list_repos()]
        return ControlCenterTechLeadPayload(
            waiting_count=_waiting_count(rows),
            # A repository that did not report now has an UNKNOWN backlog: the
            # page must not say all-clear.
            unreported_count=sum(1 for row in rows if row.availability != "available"),
            generated_at=self.clock().isoformat(),
            repos=rows,
        )

    def command(
        self, repo_key: str, payload: TechLeadProposalCommandPayload
    ) -> TechLeadCommandResult:
        repo = next(
            (item for item in self.list_repos() if configured_repository_key(item.path) == repo_key),
            None,
        )
        number = payload.proposal_issue_number
        if repo is None:
            raise UnknownTechLeadRepositoryError(repo_key)
        engine = self._running_engine(repo)
        if engine is None:
            return _refused(503, number, "The repository's engine is not running; approve on GitHub instead")
        port = engine.port
        answer = self.transport.send_command(port, payload.model_dump(mode="json"))
        if isinstance(answer, EngineCommandFailure):
            logger.warning("[tech-lead] engine command failed: %s", answer.detail)
            return _refused(503, number, f"The repository's engine did not take the decision: {answer.detail}")
        status, body = answer
        try:
            outcome = TechLeadProposalOutcomePayload.model_validate(body)
        except ValidationError:
            logger.warning("[tech-lead] engine answered a proposal command off-contract: %s", body)
            # Off the outcome contract (auth, validation, a crash) still says
            # why: the HTTP status and body are the cause (#8222 r1 F2).
            failure = off_contract_answer_failure(
                command=_DECISION_COMMAND, url=_proposals_url(port),
                upstream_status=status, body_text=json.dumps(body),
            )
            return _refused(503, number, f"The repository's engine did not take the decision: {failure.detail}")
        return TechLeadCommandResult(200 if status == 200 else 409, outcome)

    def _repo_row(self, repo: "RegisteredRepo") -> ControlCenterTechLeadRepoPayload:
        key = configured_repository_key(repo.path)
        name = repo.name or Path(repo.path).name
        engine = self._running_engine(repo)
        if engine is None:
            # Only a running engine's missed read is bridged: a restarted one
            # reports afresh.
            self.memory.engine_stopped(key)
            return ControlCenterTechLeadUnreportedRepoPayload(
                repo_key=key, name=name, availability="engine_not_running",
                detail="Engine not running: start it to see what waits here.", section=None,
            )
        read = self.memory.begin_read(key, engine)
        answer = self._read_section(repo, engine.port)
        if isinstance(answer, TechLeadPageSectionPayload):
            self.memory.answered(read, answer)
            return ControlCenterTechLeadAvailableRepoPayload(
                repo_key=key, name=name, availability="available", detail="", section=answer,
            )
        last = self.memory.last_report(read)
        if last is None:
            return ControlCenterTechLeadUnreportedRepoPayload(
                repo_key=key, name=name, availability="unavailable", detail=f"{answer}.", section=None,
            )
        # Its generated_at is the as-of time the page shows with it.
        return ControlCenterTechLeadLastKnownRepoPayload(
            repo_key=key, name=name, availability="last_known", detail=f"{answer}.", section=last,
        )

    def _read_section(self, repo: "RegisteredRepo", port: int) -> TechLeadPageSectionPayload | str:
        """The engine's section, validated against the contract, or why not."""
        raw = self.transport.read_section(port)
        if raw is None:
            return _NO_ANSWER
        try:
            return TechLeadPageSectionPayload.model_validate(raw)
        except ValidationError:
            logger.warning("[tech-lead] engine for %s answered its section off-contract", repo.path)
            return _OFF_CONTRACT

    def _running_engine(self, repo: "RegisteredRepo") -> RunningEngine | None:
        path = Path(repo.path)
        if not path.exists():
            return None
        selection = repo.launch_selection
        statuses = list(
            self.supervisor.status_all_instances(
                path, selection.config.value, mode=selection.mode.value
            ).instances
        ) or [self.supervisor.status(path)]
        for status in statuses:
            engine = RunningEngine.observed(status)
            if engine is not None:
                return engine
        return None


def build_control_center_tech_lead(
    supervisor: "SupervisorOps", memory: TechLeadSectionMemory
) -> ControlCenterTechLead:
    """Composition for the Control Center: the registry's repositories, over HTTP."""
    from ..infra import repo_registry

    return ControlCenterTechLead(supervisor, repo_registry.list_repos, HttpEngineTechLeadTransport(), memory)


def _waiting_count(rows: Sequence[ControlCenterTechLeadRepoPayload]) -> int | None:
    """What waits across the engines that reported now, or ``None`` when a
    running engine did not: its backlog is unknown, so the total is too."""
    if any(row.availability in _RUNNING_UNREPORTED for row in rows):
        return None
    return sum(
        row.section.waiting_count for row in rows if isinstance(row, ControlCenterTechLeadAvailableRepoPayload)
    )


def _refused(status: int, number: int, detail: str) -> TechLeadCommandResult:
    return TechLeadCommandResult(
        status,
        TechLeadProposalOutcomePayload(proposal_issue_number=number, outcome="unavailable", detail=detail),
    )


__all__ = [
    "ControlCenterTechLead",
    "EngineTechLeadTransport",
    "HttpEngineTechLeadTransport",
    "RunningEngine",
    "SectionRead",
    "TechLeadCommandResult",
    "TechLeadSectionMemory",
    "UnknownTechLeadRepositoryError",
    "build_control_center_tech_lead",
]
