"""Apply-time creation boundary for every tech-lead-authored issue.

Plain follow-up issues, gated operation proposals (#6778), and pattern case
files (#6781) share one GitHub creation boundary. This module owns the common
milestone resolution/event path and delegates each ledger-backed subtype's
create-once finalization without making callers know either store contract.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from ..domain.tech_lead_session import (
    PROPOSED_TECH_LEAD_LABEL,
    is_proposed_tech_lead_gate,
)
from ..events import EventName
from ..ports import make_trace_event
from .actions import (
    Action,
    ActionResult,
    CreateTechLeadCaseFileIssueAction,
    CreateTechLeadIssueAction,
    CreateTechLeadProposalIssueAction,
)
from .claim_gate import ClaimLostError
from .reconciliation import ReconciliationRequired
from .tech_lead_issue_labels import required_label_provisioning_error
from .tech_lead_proposal_creation import TechLeadProposalCreation
from .tech_lead_case_file_owner import CaseFileState, PatternCaseFileOwner
from .tech_lead_issue_policy import resolve_tech_lead_milestone_number

if TYPE_CHECKING:
    from ..ports import EventSink, RepositoryHost
    from ..ports.pattern_registry import PatternCaseFileRegistry
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .retry_history_state import ExpediteLane

logger = logging.getLogger(__name__)


def _proposal_link_comment(
    action: CreateTechLeadProposalIssueAction, issue_number: int
) -> str:
    op = action.op
    return (
        "## 🗳️ Tech Lead proposal filed as a gated issue\n\n"
        f"Proposal {op.source_action_id} (`{op.op_type}` for"
        f" #{op.target_issue_number}) was filed as #{issue_number}. It is"
        f" inert until someone removes its `{PROPOSED_TECH_LEAD_LABEL}` label"
        " (per-instance approval, ADR-0031 §2)."
    )


def _creation_preflight(
    action: CreateTechLeadIssueAction,
    *,
    repository_host: "RepositoryHost",
    ops: "TechLeadAuthorityStore | None",
    case_files: "PatternCaseFileOwner | None",
    before_case_file_write: Callable[[], None],
) -> ActionResult | None:
    """Validate ledger-backed creation and reconcile an inflight case file."""
    is_proposal = isinstance(action, CreateTechLeadProposalIssueAction)
    is_case_file = isinstance(action, CreateTechLeadCaseFileIssueAction)
    if (is_proposal or is_case_file) and ops is None:
        return ActionResult.fail(
            action,
            "gated tech_lead proposal / pattern case file requested but no"
            " TechLeadAuthorityStore is wired into this applier",
        )
    if is_case_file:
        assert isinstance(action, CreateTechLeadCaseFileIssueAction)
        assert case_files is not None
        inspected = _inspect_existing_case_file(action, case_files)
        if inspected is not None:
            return inspected
        label_error = required_label_provisioning_error(
            action,
            repository_host=repository_host,
            before_write=before_case_file_write,
        )
        if label_error is not None:
            logger.error("[APPLIER] %s", label_error)
            return ActionResult.fail(action, label_error)
    if is_case_file:
        assert isinstance(action, CreateTechLeadCaseFileIssueAction)
        assert case_files is not None
        try:
            # "Does a case file already exist for this signature?" is the
            # owner's question, not this boundary's: it checks the ledger AND
            # recovers an issue a previous attempt created before dying, so one
            # signature can never end up with two case files (#6957 R2 F10).
            resolution = case_files.resolve(action)
            if resolution.issue_number is not None:
                # Committed or just recovered: either way this action's
                # observations are appends onto the existing case file, with
                # their classification reconciled before anything is posted.
                case_files.adopt(action, issue_number=resolution.issue_number)
                return ActionResult.ok(
                    action,
                    issue_number=resolution.issue_number,
                    pr_count=action.pr_count,
                    deduplicated=True,
                    recovered=resolution.state is CaseFileState.RECOVERED,
                )
        except (ReconciliationRequired, ClaimLostError):
            raise
        except Exception as exc:
            logger.exception(
                "Failed to reconcile pattern ledger before case-file creation"
            )
            return ActionResult.fail(action, str(exc))
    return None


def _inspect_existing_case_file(
    action: CreateTechLeadCaseFileIssueAction,
    case_files: PatternCaseFileOwner,
) -> ActionResult | None:
    """Resolve an existing case file before unnecessary label provisioning."""
    try:
        inspected = case_files.inspect(action)
        if inspected is None:
            return None
        # ``inspect`` is deliberately read-only.  Let the owner pass through
        # its authoritative reservation boundary before adopting so a local
        # rolling-upgrade row that was committed just before authority was
        # lost can retire the matching creation intent on recovery.
        resolved = case_files.resolve(action)
        assert resolved.issue_number is not None
        case_files.adopt(action, issue_number=resolved.issue_number)
        return ActionResult.ok(
            action,
            issue_number=resolved.issue_number,
            pr_count=action.pr_count,
            deduplicated=True,
        )
    except (ReconciliationRequired, ClaimLostError):
        raise
    except Exception as exc:
        logger.exception("Failed to inspect pattern registry before creation")
        return ActionResult.fail(action, str(exc))


def apply_create_tech_lead_issue(
    action: CreateTechLeadIssueAction,
    *,
    repository_host: "RepositoryHost",
    events: "EventSink",
    ops: "TechLeadAuthorityStore | None",
    pattern_registry: "PatternCaseFileRegistry | None" = None,
    add_comment: Callable[[int, str], str],
    emit_labels_changed: Callable[[int, list[str], list[str]], None],
    before_case_file_write: Callable[[], None],
    proposal_guard: Callable[[Action, int], None],
    expedite_lane: "ExpediteLane | None" = None,
) -> ActionResult:
    """Create a tech_lead issue and finalize its optional authority ledger."""
    # Case-file identity (ledger lookup, remote recovery, observation accrual)
    # belongs to ONE owner; this boundary owns milestone resolution, the GitHub
    # create, and the trace event, and asks the owner for outcomes (#6957 R2 A1).
    if pattern_registry is None and ops is not None:
        from .pattern_registry import LocalPatternCaseFileRegistry

        pattern_registry = LocalPatternCaseFileRegistry(
            ops, before_write=before_case_file_write
        )
    case_files = (
        PatternCaseFileOwner(
            registry=pattern_registry,
            repository_host=repository_host,
            add_comment=add_comment,
            before_write=before_case_file_write,
        )
        if pattern_registry is not None
        else None
    )
    preflight = _creation_preflight(
        action,
        repository_host=repository_host,
        ops=ops,
        case_files=case_files,
        before_case_file_write=before_case_file_write,
    )
    if preflight is not None:
        return preflight
    try:
        milestone = resolve_tech_lead_milestone_number(
            action.milestone, repository_host.list_milestones
        )
        if isinstance(action, CreateTechLeadCaseFileIssueAction):
            # Durable creation intent BEFORE the remote create, so an
            # interrupted creation stays attributable to THIS command rather
            # than to whichever later action recovers it (#6957 R3 F10).
            assert case_files is not None
            case_files.begin(action)
        if isinstance(action, CreateTechLeadProposalIssueAction):
            assert ops is not None
            number = TechLeadProposalCreation(ops, repository_host).create(
                action, milestone, guard=proposal_guard
            )
            result = {"number": number}
        else:
            result = repository_host.create_issue(
                title=action.title,
                body=action.body,
                labels=list(action.labels),
                milestone=milestone,
            )
    except (ReconciliationRequired, ClaimLostError):
        raise
    except Exception as exc:
        logger.exception("Failed to create tech_lead issue")
        return ActionResult.fail(action, str(exc))

    issue_number = result.get("number") if result else None
    if not issue_number:
        logger.warning(
            "[APPLIER] Tech Lead issue creation returned None (title=%s labels=%s)",
            action.title,
            list(action.labels),
        )
        return ActionResult.fail(action, "Issue creation returned None")

    logger.info(
        "[APPLIER] Created tech_lead issue #%d for %d PRs (milestone=%s)",
        issue_number,
        action.pr_count,
        milestone,
    )
    emit_labels_changed(issue_number, list(action.labels), [])
    events.publish(
        make_trace_event(
            EventName.TECH_LEAD_ISSUE_CREATED,
            {
                "issue_number": issue_number,
                "pr_count": action.pr_count,
                # Why this anchor exists, and what it consumed. A storm
                # escalation collapses N individual investigations into one
                # review; without these the only trace of that decision is log
                # text, which UI and tests must never parse. ``flavor`` is the
                # authoring owner's decision, carried on the action — this
                # boundary reports it and never reinterprets marker labels.
                "trigger": action.reason,
                "storm_problem_count": len(action.storm_problems),
                "flavor": action.flavor.value,
            },
        )
    )
    _apply_expedite_lane(action, issue_number=issue_number, expedite_lane=expedite_lane)
    finalization_error = _finalize_ledger_backed_creation(
        action,
        issue_number=issue_number,
        ops=ops,
        case_files=case_files,
        add_comment=add_comment,
    )
    if finalization_error is not None:
        return ActionResult.fail(action, finalization_error, issue_number=issue_number)
    return ActionResult.ok(
        action,
        issue_number=issue_number,
        pr_count=action.pr_count,
    )


def _apply_expedite_lane(
    action: CreateTechLeadIssueAction,
    *,
    issue_number: int,
    expedite_lane: "ExpediteLane | None",
) -> None:
    """Route an expedite-marked create_issue onto the worker lane (#6870).

    Inherits the ADR-0031 create_issue gate rather than bypassing it: a GATED
    (propose-authority) creation carries ``proposed-tech-lead``, so it is only
    DEFERRED here — the planning cycle promotes it once an operator removes the
    gate. An UNGATED (execute-authority) creation jumps the lane immediately.
    Either way the write goes through the ExpediteLane owner, never a direct
    priority_queue mutation, and the cap is enforced there. Unwired lane (tests
    / no orchestrator) or a non-expedite action is a no-op.
    """
    if not action.expedite or expedite_lane is None:
        return
    gated = any(is_proposed_tech_lead_gate(name) for name in action.labels)
    if gated:
        expedite_lane.defer_until_ungated(issue_number)
    else:
        expedite_lane.expedite_now(issue_number)


def _finalize_ledger_backed_creation(
    action: CreateTechLeadIssueAction,
    *,
    issue_number: int,
    ops: "TechLeadAuthorityStore | None",
    case_files: "PatternCaseFileOwner | None",
    add_comment: Callable[[int, str], str],
) -> str | None:
    """Record a proposal/case-file ledger row; return a failure message."""
    try:
        if isinstance(action, CreateTechLeadProposalIssueAction):
            assert ops is not None
            add_comment(
                action.anchor_issue_number,
                _proposal_link_comment(action, issue_number),
            )
        elif isinstance(action, CreateTechLeadCaseFileIssueAction):
            assert case_files is not None
            case_files.open(action, issue_number=issue_number)
    except (ReconciliationRequired, ClaimLostError):
        raise
    except Exception as exc:
        logger.exception(
            "Failed to finalize ledger-backed tech_lead issue #%d", issue_number
        )
        return str(exc)
    return None
