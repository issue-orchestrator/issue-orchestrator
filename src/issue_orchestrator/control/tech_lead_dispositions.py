"""Terminal investigation policy, publication, and durable recovery waits.

Admission is persisted before the guarded explanation. Tick replay consumes that
trusted pending command even after session cleanup; a conditional commit makes
it a waiting binding. Neither a partial publication nor a stale remedy proves a
successful investigation. The sweep owns bounded waiting and positive recovery.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable

from ..domain.dependencies import parse_dependency_edges
from ..domain.tech_lead_session import TechLeadSessionFlavor
from .action_results import ActionResult, ActionResultType
from .tech_lead_actions import RecordTechLeadDispositionAction

# Compatibility exports keep the sweep's owner import stable.
from .tech_lead_disposition_ledger import (
    NO_TECH_LEAD_DISPOSITIONS,
    StuckSweepDispositions,
    TechLeadDispositionLedger,
    build_disposition_ledger,
)


if TYPE_CHECKING:
    from ..domain.tech_lead_artifacts import TechLeadDecision
    from ..domain.tech_lead_session import TechLeadDisposition, TechLeadLaunchAuthority
    from ..ports import RepositoryHost
    from ..ports.issue import Issue
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .actions import Action

logger = logging.getLogger(__name__)
TERMINAL_INVESTIGATION_ACTIONS = frozenset(
    {
        "defer_to_tracker",
        "escalate_to_human",
        "reset_retry",
        "kill_hung_session",
    }
)


def recovery_tracker_grants(issue: "Issue") -> tuple[int, ...]:
    """Grant only explicit, same-repository prerequisite edges, never mentions."""
    return tuple(
        sorted(
            {
                edge.issue_number
                for edge in parse_dependency_edges(issue.body or "")
                if edge.issue_number is not None
                and edge.repository is None
                and edge.issue_number != issue.number
            }
        )
    )


def investigation_disposition_violation(
    decision: "TechLeadDecision", authority: "TechLeadLaunchAuthority"
) -> str | None:
    """One terminal remedy for the focus job; agent output cannot widen scope."""
    for action in decision.proposed_actions:
        if action.action_type == "defer_to_tracker":
            if authority.flavor is not TechLeadSessionFlavor.FAILURE_INVESTIGATION:
                return "defer_to_tracker is valid only for a failure investigation"
            if (
                action.target_number != authority.focus_issue_number
                or action.tracker_number not in authority.recovery_tracker_numbers
            ):
                return "defer_to_tracker requires the focus issue and a launch-granted recovery tracker"
    if authority.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION:
        terminal = [
            a
            for a in decision.proposed_actions
            if a.action_type in TERMINAL_INVESTIGATION_ACTIONS
            and a.target_number == authority.focus_issue_number
        ]
        if len(terminal) != 1:
            return "failure investigation requires exactly one terminal disposition for the focus issue: defer_to_tracker, escalate_to_human, reset_retry, or kill_hung_session"
        assert authority.focus_issue_number is not None
        if terminal[0].action_type == "kill_hung_session" and authority.observed_kill_target(authority.focus_issue_number) is None:
            return "kill_hung_session requires a launch-observed worker generation"
    return None


def disposition_marker(disposition: "TechLeadDisposition") -> str:
    identity = f"{disposition.source_run_id}:{disposition.source_session_name}:{disposition.source_action_id}"
    return f"<!-- io:tech-lead-disposition:{hashlib.sha256(identity.encode()).hexdigest()} -->"


def reassess_at(disposition: "TechLeadDisposition") -> datetime:
    return disposition.reassess_at


def disposition_comment(disposition: "TechLeadDisposition") -> str:
    return (
        "## Tech Lead disposition — awaiting recovery\n\n"
        f"The remedy for this issue is tracked by #{disposition.tracker_issue_number}. "
        "The orchestrator will retain this binding after this explanation is "
        "published and the disposition command commits.\n\n"
        f"{disposition.rationale}\n\n"
        f"Reassessment is due by {reassess_at(disposition).isoformat()}, or sooner "
        f"when #{disposition.tracker_issue_number} closes or disappears. "
        "Clearing this issue's blocking state releases the binding and resets "
        "its recovery budget. A read outage preserves an active binding only until its deadline.\n\n"
        f"Action {disposition.source_action_id}.\n{disposition_marker(disposition)}"
    )


def _admit_disposition(
    disposition: "TechLeadDisposition", authority: "TechLeadAuthorityStore",
) -> None:
    grant = authority.load(
        run_id=disposition.source_run_id, session_name=disposition.source_session_name
    )
    if (
        grant is None
        or grant.flavor is not TechLeadSessionFlavor.FAILURE_INVESTIGATION
        or grant.focus_issue_number != disposition.issue_number
        or disposition.tracker_issue_number not in grant.recovery_tracker_numbers
    ):
        raise ValueError("disposition has no immutable launch grant for this target/tracker")


def _validate_live_wait(disposition: "TechLeadDisposition", host: "RepositoryHost") -> None:
    issue = host.get_issue(disposition.issue_number)
    if issue is None or issue.state != "open":
        raise ValueError("disposition target is missing or closed")
    if disposition.tracker_issue_number not in recovery_tracker_grants(issue):
        raise ValueError("recovery tracker is no longer a declared prerequisite")
    if host.get_issue_state(disposition.tracker_issue_number) != "open":
        raise ValueError("recovery tracker is missing or closed")


def _prepare(
    proposed: "TechLeadDisposition", authority: "TechLeadAuthorityStore", now: datetime,
    host: "RepositoryHost",
) -> "TechLeadDisposition":
    """Persist trusted admission BEFORE any remote effect; exact replays need no session."""
    previous = authority.load_disposition(issue_number=proposed.issue_number)
    if previous is not None and disposition_marker(previous) == disposition_marker(proposed):
        normalized = replace(proposed, recorded_at=previous.recorded_at,
            phase=previous.phase, recovered_at=previous.recovered_at)
        if normalized != previous:
            raise ValueError("disposition replay changed its immutable command payload")
        if previous.phase not in {"prepared", "waiting"}:
            raise ValueError("disposition already lapsed or recovered")
        return previous
    _admit_disposition(proposed, authority)
    if previous is not None and previous.phase != "recovered":
        raise ValueError("this incident already has a disposition; reassess or remediate it")
    if previous is not None and (
        (previous.source_run_id, previous.source_session_name) == (proposed.source_run_id, proposed.source_session_name)
        or datetime.fromisoformat(proposed.recorded_at) <= datetime.fromisoformat(previous.recovered_at)
    ):
        raise ValueError("a recovered incident requires a newly launched investigation")
    prepared = replace(proposed, phase="prepared", recovered_at="")
    if now >= reassess_at(prepared):
        raise ValueError("recovery deadline expired; choose remediation or human escalation")
    _validate_live_wait(prepared, host)
    if not authority.transition_disposition(previous=previous, disposition=prepared):
        raise ValueError("disposition admission raced another incident transition")
    return prepared


@dataclass(frozen=True)
class _DispositionPublisher:
    """An exclusive publisher's collaborators; admission stays with the lifecycle."""

    authority: "TechLeadAuthorityStore"
    host: "RepositoryHost"
    apply_action: Callable[["Action"], ActionResult]
    require_expected: Callable[["Action", int], None]
    verify_claim: Callable[["Action", int], None]
    clock: Callable[[], datetime]

    def _revalidate(self, action: "Action", disposition: "TechLeadDisposition") -> None:
        if self.authority.load_disposition(issue_number=disposition.issue_number) != disposition:
            raise ValueError("disposition changed before publication; stale publisher refused")
        if self.clock() >= disposition.reassess_at:
            self.authority.transition_disposition(previous=disposition,
                disposition=replace(disposition, phase="reassess"))
            raise ValueError("recovery deadline expired; reassessment required")
        _validate_live_wait(disposition, self.host)
        self.require_expected(action, disposition.issue_number)
        self.verify_claim(action, disposition.issue_number)

    def publish(self, action: "Action", disposition: "TechLeadDisposition") -> None:
        from .actions import AddCommentAction
        self._revalidate(action, disposition)
        present = self.host.issue_comment_marker_present(disposition.issue_number, disposition_marker(disposition))
        # Recheck after even a marker query: time, pause, or row ownership may
        # have changed while the remote read was outstanding.
        self._revalidate(action, disposition)
        if not present:
            result = self.apply_action(AddCommentAction(number=disposition.issue_number,
                comment=disposition_comment(disposition), reason=action.reason, expected=action.expected))
            if result.result_type is not ActionResultType.SUCCESS:
                raise ValueError(result.error or "disposition explanation did not commit")
        self._revalidate(action, disposition)
        if not self.authority.transition_disposition(previous=disposition,
            disposition=replace(disposition, phase="waiting")):
            raise ValueError("disposition changed during publication; reassessment required")


def apply_record_tech_lead_disposition(
    action: "Action", *, authority: "TechLeadAuthorityStore | None",
    repository_host: "RepositoryHost | None",
    apply_action: Callable[["Action"], ActionResult],
    require_expected: Callable[["Action", int], None],
    verify_claim: Callable[["Action", int], None], clock: Callable[[], datetime],
) -> ActionResult:
    """Resume one exclusively owned command; only committed waiting proves success."""
    assert isinstance(action, RecordTechLeadDispositionAction)
    if authority is None or repository_host is None:
        return ActionResult.fail(action, "disposition requires TechLeadAuthorityStore and RepositoryHost")
    disposition = action.disposition
    assert disposition is not None
    pending = False
    try:
        require_expected(action, disposition.issue_number)
        verify_claim(action, disposition.issue_number)
        disposition = _prepare(disposition, authority, clock(), repository_host)
        pending = disposition.phase == "prepared"
        with authority.disposition_publication(issue_number=disposition.issue_number) as acquired:
            if not acquired:
                raise ValueError("another publisher owns this disposition; retry on a later tick")
            _DispositionPublisher(authority, repository_host, apply_action,
                require_expected, verify_claim, clock).publish(action, disposition)
    except Exception as exc:
        logger.exception("Could not commit disposition for #%d", disposition.issue_number)
        return ActionResult.fail(action, str(exc), pending_disposition=pending)
    return ActionResult.ok(action, issue_number=disposition.issue_number,
        tracker_issue_number=disposition.tracker_issue_number)


__all__ = ["NO_TECH_LEAD_DISPOSITIONS", "StuckSweepDispositions", "TechLeadDispositionLedger", "build_disposition_ledger"]
