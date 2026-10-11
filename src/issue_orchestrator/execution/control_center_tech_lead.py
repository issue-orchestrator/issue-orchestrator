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
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol, Sequence

from pydantic import ValidationError

from ..contracts.ui_openapi_models import (
    ControlCenterTechLeadPayload,
    ControlCenterTechLeadRepoPayload,
    TechLeadPageSectionPayload,
    TechLeadProposalCommandPayload,
    TechLeadProposalOutcomePayload,
)
from ..infra.repo_identity import configured_repository_key
from .engine_command_failure import ENGINE_COMMAND_TIMEOUT_SECONDS, EngineCommandFailure, off_contract_answer_failure
from .orchestrator_http_api import post_orchestrator_json, probe_orchestrator_json

if TYPE_CHECKING:
    from ..infra.repo_registry import RegisteredRepo
    from ..ports.repository_engine_supervisor import SupervisorOps

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
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def page(self) -> ControlCenterTechLeadPayload:
        rows = [self._repo_row(repo) for repo in self.list_repos()]
        return ControlCenterTechLeadPayload(
            waiting_count=sum(row.section.waiting_count for row in rows if row.section is not None),
            # A repository that did not report has an UNKNOWN backlog: the
            # count is then a lower bound and the page must not say all-clear.
            unreported_count=sum(1 for row in rows if row.section is None),
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
        port = self._running_port(repo)
        if port is None:
            return _refused(503, number, "The repository's engine is not running; approve on GitHub instead")
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
        port = self._running_port(repo)
        if port is None:
            return ControlCenterTechLeadRepoPayload(
                repo_key=key, name=name, availability="engine_not_running",
                detail="Engine not running: start it to see what waits here.", section=None,
            )
        raw = self.transport.read_section(port)
        if raw is None:
            return ControlCenterTechLeadRepoPayload(
                repo_key=key, name=name, availability="unavailable",
                detail="The engine is running but did not answer.", section=None,
            )
        try:
            section = TechLeadPageSectionPayload.model_validate(raw)
        except ValidationError:
            logger.warning("[tech-lead] engine for %s answered its section off-contract", repo.path)
            return ControlCenterTechLeadRepoPayload(
                repo_key=key, name=name, availability="unavailable",
                detail="The engine's answer was not understood (version mismatch?).", section=None,
            )
        return ControlCenterTechLeadRepoPayload(
            repo_key=key, name=name, availability="available", detail="", section=section,
        )

    def _running_port(self, repo: "RegisteredRepo") -> int | None:
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
            if status.state == "running" and status.port:
                return status.port
        return None


def build_control_center_tech_lead(supervisor: "SupervisorOps") -> ControlCenterTechLead:
    """Composition for the Control Center: the registry's repositories, over HTTP."""
    from ..infra import repo_registry

    return ControlCenterTechLead(supervisor, repo_registry.list_repos, HttpEngineTechLeadTransport())


def _refused(status: int, number: int, detail: str) -> TechLeadCommandResult:
    return TechLeadCommandResult(
        status,
        TechLeadProposalOutcomePayload(proposal_issue_number=number, outcome="unavailable", detail=detail),
    )


__all__ = [
    "ControlCenterTechLead",
    "EngineTechLeadTransport",
    "HttpEngineTechLeadTransport",
    "TechLeadCommandResult",
    "UnknownTechLeadRepositoryError",
    "build_control_center_tech_lead",
]
