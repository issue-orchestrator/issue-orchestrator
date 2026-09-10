"""Synchronize operator proposal requests with the engine tick.

The facade resolves its composed owner here; web routes see only typed engine
commands. Proposal policy remains entirely in the stored-op owner.

Like tech-lead admission and launch, these gestures run under the reentrant
state lock: the dashboard thread and the engine tick must not interleave
consent or execution. The raw queued launch remains admission-owner-only.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from typing import TYPE_CHECKING

from ..control.scoped_rework import RequestReworkExecutor
from ..control.scoped_rework_proposals import ReworkProposalView
from ..domain.scoped_rework import (
    TechLeadProposalCommand,
    TechLeadProposalCommandOutcome,
)

if TYPE_CHECKING:
    from .orchestrator import Orchestrator


@contextmanager
def _proposal_owner(orchestrator: Orchestrator) -> Iterator[RequestReworkExecutor]:
    with orchestrator.state_lock:
        owner = orchestrator.deps.action_applier.request_rework
        if owner is None:
            raise RuntimeError("Scoped rework executor is not wired")
        yield owner


def rework_proposal_views(orchestrator: Orchestrator) -> tuple[ReworkProposalView, ...]:
    with _proposal_owner(orchestrator) as owner:
        return owner.proposal_views()


def rework_proposal_command(
    orchestrator: Orchestrator, command: TechLeadProposalCommand
) -> TechLeadProposalCommandOutcome:
    with _proposal_owner(orchestrator) as owner:
        return owner.proposal_command(command)
