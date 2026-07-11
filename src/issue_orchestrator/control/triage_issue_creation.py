"""Apply-time creation boundary for every triage-authored issue.

Plain follow-up issues, gated operation proposals (#6778), and pattern case
files (#6781) share one GitHub creation boundary. This module owns the common
milestone resolution/event path and delegates each ledger-backed subtype's
create-once finalization without making callers know either store contract.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from ..domain.triage_session import PROPOSED_TRIAGE_LABEL
from ..events import EventName
from ..ports import make_trace_event
from .actions import (
    ActionResult,
    CreateTriageCaseFileIssueAction,
    CreateTriageIssueAction,
    CreateTriageProposalIssueAction,
)
from .triage_issue_policy import resolve_triage_milestone_number

if TYPE_CHECKING:
    from ..ports import EventSink, RepositoryHost
    from ..ports.triage_authority import TriageAuthorityStore

logger = logging.getLogger(__name__)


def _proposal_gate_provisioning_error(
    repository_host: "RepositoryHost",
) -> str | None:
    """Reason the proposal gate is unusable, or None when provisioned."""
    try:
        existing = {
            name.casefold()
            for entry in repository_host.list_labels()
            if isinstance(entry, dict)
            and isinstance((name := entry.get("name")), str)
        }
    except Exception as exc:
        return (
            f"could not verify the {PROPOSED_TRIAGE_LABEL!r} gate label is"
            f" provisioned; refusing to create an ungated proposal: {exc}"
        )
    if PROPOSED_TRIAGE_LABEL.casefold() not in existing:
        return (
            f"the {PROPOSED_TRIAGE_LABEL!r} gate label is not provisioned in"
            " this repository; run `issue-orchestrator init` to create it."
            " Refusing to create an ungated triage proposal (#6779 R3)"
        )
    return None


def _proposal_link_comment(
    action: CreateTriageProposalIssueAction, issue_number: int
) -> str:
    op = action.op
    return (
        "## 🗳️ Triage proposal filed as a gated issue\n\n"
        f"Proposal {op.source_action_id} (`{op.op_type}` for"
        f" #{op.target_issue_number}) was filed as #{issue_number}. It is"
        f" inert until someone removes its `{PROPOSED_TRIAGE_LABEL}` label"
        " (per-instance approval, ADR-0031 §2)."
    )


def _creation_preflight(
    action: CreateTriageIssueAction,
    *,
    repository_host: "RepositoryHost",
    ops: "TriageAuthorityStore | None",
    add_comment: Callable[[int, str], str],
) -> ActionResult | None:
    """Validate ledger-backed creation and reconcile an inflight case file."""
    is_proposal = isinstance(action, CreateTriageProposalIssueAction)
    is_case_file = isinstance(action, CreateTriageCaseFileIssueAction)
    if (is_proposal or is_case_file) and ops is None:
        return ActionResult.fail(
            action,
            "gated triage proposal / pattern case file requested but no"
            " TriageAuthorityStore is wired into this applier",
        )
    if is_proposal:
        gate_error = _proposal_gate_provisioning_error(repository_host)
        if gate_error is not None:
            logger.error("[APPLIER] %s", gate_error)
            return ActionResult.fail(action, gate_error)
    if not is_case_file:
        return None
    assert isinstance(action, CreateTriageCaseFileIssueAction)
    assert ops is not None
    try:
        existing = ops.lookup_pattern(signature=action.pattern_signature)
        if existing is None:
            return None
        for comment in (action.dedup_comment, *action.additional_observation_comments):
            add_comment(existing, comment)
        return ActionResult.ok(
            action,
            issue_number=existing,
            pr_count=action.pr_count,
            deduplicated=True,
        )
    except Exception as exc:
        logger.exception("Failed to reconcile pattern ledger before case-file creation")
        return ActionResult.fail(action, str(exc))


def apply_create_triage_issue(
    action: CreateTriageIssueAction,
    *,
    repository_host: "RepositoryHost",
    events: "EventSink",
    ops: "TriageAuthorityStore | None",
    add_comment: Callable[[int, str], str],
    emit_labels_changed: Callable[[int, list[str], list[str]], None],
) -> ActionResult:
    """Create a triage issue and finalize its optional authority ledger."""
    preflight = _creation_preflight(
        action,
        repository_host=repository_host,
        ops=ops,
        add_comment=add_comment,
    )
    if preflight is not None:
        return preflight
    try:
        milestone = resolve_triage_milestone_number(
            action.milestone, repository_host.list_milestones
        )
        result = repository_host.create_issue(
            title=action.title,
            body=action.body,
            labels=list(action.labels),
            milestone=milestone,
        )
    except Exception as exc:
        logger.exception("Failed to create triage issue")
        return ActionResult.fail(action, str(exc))

    issue_number = result.get("number") if result else None
    if not issue_number:
        logger.warning(
            "[APPLIER] Triage issue creation returned None (title=%s labels=%s)",
            action.title,
            list(action.labels),
        )
        return ActionResult.fail(action, "Issue creation returned None")

    logger.info(
        "[APPLIER] Created triage issue #%d for %d PRs (milestone=%s)",
        issue_number,
        action.pr_count,
        milestone,
    )
    emit_labels_changed(issue_number, list(action.labels), [])
    events.publish(
        make_trace_event(
            EventName.TRIAGE_ISSUE_CREATED,
            {"issue_number": issue_number, "pr_count": action.pr_count},
        )
    )
    finalization_error = _finalize_ledger_backed_creation(
        action,
        issue_number=issue_number,
        ops=ops,
        add_comment=add_comment,
    )
    if finalization_error is not None:
        return ActionResult.fail(action, finalization_error, issue_number=issue_number)
    return ActionResult.ok(
        action,
        issue_number=issue_number,
        pr_count=action.pr_count,
    )


def _finalize_ledger_backed_creation(
    action: CreateTriageIssueAction,
    *,
    issue_number: int,
    ops: "TriageAuthorityStore | None",
    add_comment: Callable[[int, str], str],
) -> str | None:
    """Record a proposal/case-file ledger row; return a failure message."""
    try:
        if isinstance(action, CreateTriageProposalIssueAction):
            assert ops is not None
            ops.record_op(issue_number=issue_number, op=action.op)
            add_comment(
                action.anchor_issue_number,
                _proposal_link_comment(action, issue_number),
            )
        elif isinstance(action, CreateTriageCaseFileIssueAction):
            assert ops is not None
            ops.record_pattern(
                signature=action.pattern_signature, issue_number=issue_number
            )
            for comment in action.additional_observation_comments:
                add_comment(issue_number, comment)
    except Exception as exc:
        logger.exception("Failed to finalize ledger-backed triage issue #%d", issue_number)
        return str(exc)
    return None
