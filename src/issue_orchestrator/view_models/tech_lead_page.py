"""One engine's section of the Control Center's Tech lead page (#7763).

Presentation only, and pure: :class:`TechLeadPageInputs` carries what the engine
already holds (the approval owner's last observed scope, the op ledger, the
charter decision ledger, the action liveness owner's parks, needs-human cause
rows, the tick's cached issues, the board's case files, the run history), and
:func:`build_tech_lead_page_section` projects it onto the generated
:class:`~..contracts.ui_openapi_models.TechLeadPageSectionPayload`. Nothing here
reads GitHub or decides anything: approval policy lives in the approval owner,
custody in its owner, and every status string is rendered verbatim so the page
is legible without a colour key.

Three lanes, oldest first where time matters to the operator:

* **waiting** — tech-lead proposals (every kind), PRs whose merge is held for a
  person, and items the tech lead handed to a person;
* **doing** — what the tech lead did on its own in the last day, in flight
  first, plus actions the liveness owner parked;
* **watching** — blocked items it triaged, case files it tracks, and when the
  next health review is due.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

from ..contracts.ui_openapi_models import (
    TechLeadCaseFilePayload,
    TechLeadDetailRowPayload,
    TechLeadDoingItemPayload,
    TechLeadHealthReviewPayload,
    TechLeadPageSectionPayload,
    TechLeadParkedActionPayload,
    TechLeadRunStripPayload,
    TechLeadTriagedItemPayload,
    TechLeadWaitingItemPayload,
)
from ..domain.human_block import NeedsHumanCause
from ..domain.tech_lead_approval import ApprovalVerdict, proposal_state
from ..domain.tech_lead_charter import CharterOutcome
from ..domain.tech_lead_charter_decisions import (
    CharterProposalLifecycle,
    TechLeadCharterDecision,
)

if TYPE_CHECKING:
    from ..control.merge_hold_status import MergeHoldStatus
    from ..domain.action_liveness import LivenessRow
    from ..domain.tech_lead_session import StoredTechLeadOp, TechLeadCaseFileSummary
    from ..ports.issue import Issue
    from ..ports.pending_work_claim_store import NeedsHumanCauseRow
    from .tech_lead_activity import TechLeadRunActivityEntry

#: How far back the "doing on its own" lane reaches.
DOING_WINDOW = timedelta(hours=24)
#: Causes of the shared needs-human block that hold only a MERGE for a person.
#: #7712 adds the agent's merge decision here as a cause of its own.
MERGE_HOLD_CAUSES: frozenset[str] = frozenset({NeedsHumanCause.MERGE_ESCALATION.value})

_RECOMMENDATION_CHARS = 200

_APPROVAL_EFFECTS: dict[str, str] = {
    "request_rework": "Queues a scoped rework of the PR on its own branch, after re-checking the PR head is unchanged.",
    "reset_retry": "Resets #{target} and retries it from scratch. Destructive: the branch and PR are discarded.",
    "kill_hung_session": "Terminates the hung session on #{target}, only if it is still the session observed.",
    "recover_validated_work": "Publishes #{target}'s retained validated work as a PR, if nothing changed since.",
    "release_withheld_review": "Releases the code review that #{target}'s own block withholds.",
}
_FOLLOW_UP_EFFECT = "Admits this issue to the work queue, where it is worked like any other."
_MERGE_EFFECT = (
    "Nothing to approve here: merge the PR on GitHub (or fix what blocks it). The"
    " engine resumes once its needs-human label comes off."
)
_HAND_OVER_EFFECT = (
    "The tech lead handed this item to a person. Act on GitHub, then remove"
    " needs-human to give it back to the engine."
)

_TRIAGE_LABELS = {
    "operator_decision": "Decision proposed",
    "human_hand_over": "Handed to a person",
    "explained": "Explained",
    "remedy": "Remedy applied",
}


#: Receipt states in which approved rework is still moving.
REWORK_IN_FLIGHT = frozenset({"queued", "executing", "active"})


@dataclass(frozen=True)
class ReworkReceiptView:
    """One approved scoped rework's receipt, with its live status resolved
    by :func:`~..control.scoped_rework_receipt_status.rework_receipt_status`."""

    proposal_issue_number: int
    request_key: str
    issue_number: int
    status: str
    detail: str
    at: str = ""


@dataclass(frozen=True)
class TechLeadPageInputs:
    """Everything one section is built from: engine-held state, no reads."""

    repository: str
    now: datetime
    proposals: Sequence[tuple["Issue", ApprovalVerdict | None]]
    ops: Mapping[int, "StoredTechLeadOp"]
    rework_receipts: Sequence[ReworkReceiptView]
    needs_human_causes: Sequence["NeedsHumanCauseRow"]
    issues: Sequence["Issue"]
    tech_lead_needs_human_label: str
    blocked_numbers: frozenset[int]
    decisions: Sequence[TechLeadCharterDecision]
    parked: Sequence["LivenessRow"]
    case_files: Sequence["TechLeadCaseFileSummary"]
    health_interval_minutes: int
    last_health_review_at: float
    latest_run: "TechLeadRunActivityEntry | None"
    #: GitHub's view of each merge-held PR (``MergeHoldStatuses``), keyed by PR.
    merge_statuses: Mapping[int, "MergeHoldStatus"]


def issue_link(repository: str, number: int) -> str:
    return f"https://github.com/{repository}/issues/{number}"


#: Waiting-lane states that need the operator. An approved proposal waits on
#: the engine, not on them, so it is listed but not counted.
_NEEDS_OPERATOR = frozenset({"awaiting_approval", "approval_not_accepted", "merge_held", "handed_over"})


def build_tech_lead_page_section(inputs: TechLeadPageInputs) -> TechLeadPageSectionPayload:
    waiting = _waiting(inputs)
    return TechLeadPageSectionPayload(
        repository=inputs.repository,
        generated_at=inputs.now.isoformat(),
        waiting_count=sum(1 for item in waiting if item.status in _NEEDS_OPERATOR),
        run=_run_strip(inputs.latest_run),
        waiting=waiting,
        doing=_doing(inputs),
        parked=[_parked(inputs.repository, row) for row in inputs.parked],
        triaged=_triaged(inputs),
        case_files=[
            TechLeadCaseFilePayload(
                issue_number=item.issue_number,
                title=item.title,
                area=item.area,
                comment_count=item.comment_count,
                updated_at=item.updated_at,
                link=issue_link(inputs.repository, item.issue_number),
            )
            for item in inputs.case_files
        ],
        health_review=_health_review(inputs),
    )


# -- waiting on you -----------------------------------------------------------


def _waiting(inputs: TechLeadPageInputs) -> list[TechLeadWaitingItemPayload]:
    # An ADMITTED proposal is ordinary work now (#7763): only proposals still
    # gated are waiting, a marker-only one (every gate label stripped) too.
    items = [
        _proposal(inputs, issue, verdict)
        for issue, verdict in inputs.proposals
        if issue.state == "open" and proposal_state(issue.labels, issue.body).gate_closed
    ]
    proposal_numbers = {item.number for item in items}
    by_number = {issue.number: issue for issue in inputs.issues}
    merge_rows: dict[int, "NeedsHumanCauseRow"] = {}
    for row in inputs.needs_human_causes:
        if row.cause in MERGE_HOLD_CAUSES:
            merge_rows.setdefault(row.issue_number, row)
    for number, row in merge_rows.items():
        status = inputs.merge_statuses.get(number)
        if status is not None and not status.held:
            continue  # merged, closed or released since the engine stopped
        items.append(_merge_ready(inputs.repository, row, by_number.get(number), status))
    hand_over = inputs.tech_lead_needs_human_label.casefold()
    reasons = {
        row.issue_number: row.reason
        for row in inputs.needs_human_causes
        if row.cause == NeedsHumanCause.TECH_LEAD_ESCALATION.value
    }
    for issue in inputs.issues:
        if issue.state != "open" or issue.number in proposal_numbers or issue.number in merge_rows:
            continue
        if any(str(label).casefold() == hand_over for label in issue.labels):
            items.append(_hand_over(inputs.repository, issue, reasons.get(issue.number, "")))
    # Oldest first; an item with no known time sorts after the dated ones.
    return sorted(items, key=lambda item: (item.waiting_since == "", item.waiting_since, item.number))


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:_RECOMMENDATION_CHARS]
    return ""


def _proposal(
    inputs: TechLeadPageInputs, issue: "Issue", verdict: ApprovalVerdict | None
) -> TechLeadWaitingItemPayload:
    op = inputs.ops.get(issue.number)
    details: list[TechLeadDetailRowPayload] = []
    receipt = None
    if op is None:
        operation = "follow_up"
        recommendation = issue.title
        effect = _FOLLOW_UP_EFFECT
    else:
        operation = op.op_type
        effect = _APPROVAL_EFFECTS.get(op.op_type, "Executes the stored `{op}` for #{target} once, after re-validating it.")
        effect = effect.format(target=op.target_issue_number, op=op.op_type)
        recommendation = _first_line(op.rationale) or issue.title
        details.append(TechLeadDetailRowPayload(label="Target", value=f"#{op.target_issue_number}"))
        if op.decision is not None:
            recommendation = op.decision.title
            effect = (
                f"Files {len(op.decision.follow_ups)} follow-up issue(s), posts this decision on"
                f" #{op.target_issue_number}, then retries #{op.target_issue_number}."
            )
        if op.rework_request is not None:
            key = op.rework_request.key
            receipt = next((item for item in inputs.rework_receipts if item.request_key == key), None)
            details.extend(_rework_details(op))
    status, label = _proposal_status(verdict, receipt)
    open_for_decision = receipt is None
    return TechLeadWaitingItemPayload(
        kind="proposal",
        number=issue.number,
        operation=operation,
        title=issue.title,
        recommendation=recommendation,
        approval_effect=effect,
        link=issue_link(inputs.repository, issue.number),
        waiting_since=issue.created_at or "",
        status=status,
        status_label=label,
        can_approve=open_for_decision and status in ("awaiting_approval", "approval_not_accepted"),
        can_decline=open_for_decision,
        details=details,
    )


def _rework_details(op: "StoredTechLeadOp") -> list[TechLeadDetailRowPayload]:
    assert op.rework_request is not None
    request = op.rework_request
    target = request.target
    return [
        TechLeadDetailRowPayload(label="PR", value=f"{target.repository}#{target.pr_number}"),
        TechLeadDetailRowPayload(label="Expected head", value=target.head_sha),
        TechLeadDetailRowPayload(label="Evidence", value=request.evidence_identity),
        TechLeadDetailRowPayload(label="Feedback", value=request.feedback),
        TechLeadDetailRowPayload(
            label="Effects",
            value=(
                f"Preserve branch {target.branch}; invalidate code-reviewed and"
                " tech-lead-reviewed; queue normal rework. A merged PR gets one forward fix."
            ),
        ),
        TechLeadDetailRowPayload(label="Review report", value=request.report),
    ]


ProposalStatus = Literal["awaiting_approval", "approved", "approval_not_accepted", "executing"]


def _proposal_status(
    verdict: ApprovalVerdict | None, receipt: ReworkReceiptView | None
) -> tuple[ProposalStatus, str]:
    if receipt is not None:
        return "executing", f"Approved; rework {receipt.status.replace('_', ' ')}: {receipt.detail}"
    if verdict is None:
        return "awaiting_approval", "Awaiting your approval"
    if verdict.approved:
        return "approved", f"Approved ({verdict.describe()}); the engine will act on it"
    return "approval_not_accepted", f"Approval not accepted: {verdict.describe()}"


def _merge_ready(
    repository: str,
    row: "NeedsHumanCauseRow",
    issue: "Issue | None",
    status: "MergeHoldStatus | None",
) -> TechLeadWaitingItemPayload:
    details = [TechLeadDetailRowPayload(label="Why the engine stopped", value=row.reason)] if row.reason else []
    if status is not None:
        details.append(TechLeadDetailRowPayload(label="Mergeability", value=status.mergeability))
        details.append(TechLeadDetailRowPayload(label="Checks", value=status.checks))
        if status.read_error:
            details.append(TechLeadDetailRowPayload(label="GitHub read failed", value=status.read_error))
    title = issue.title if issue is not None else status.title if status is not None else f"#{row.issue_number}"
    return TechLeadWaitingItemPayload(
        kind="merge_ready_pr",
        number=row.issue_number,
        operation=row.cause,
        title=title,
        recommendation=row.reason or "Its merge is held for a person.",
        approval_effect=_MERGE_EFFECT,
        link=issue_link(repository, row.issue_number),
        waiting_since=(issue.updated_at or "") if issue is not None else "",
        status="merge_held",
        status_label="Merge held for a person",
        can_approve=False,
        can_decline=False,
        details=details,
    )


def _hand_over(repository: str, issue: "Issue", reason: str) -> TechLeadWaitingItemPayload:
    return TechLeadWaitingItemPayload(
        kind="hand_over",
        number=issue.number,
        operation="hand_over",
        title=issue.title,
        recommendation=reason or "The tech lead handed this item to a person.",
        approval_effect=_HAND_OVER_EFFECT,
        link=issue_link(repository, issue.number),
        waiting_since=issue.updated_at or "",
        status="handed_over",
        status_label="Handed to you",
        can_approve=False,
        can_decline=False,
        details=[],
    )


# -- doing on its own ---------------------------------------------------------


def _parse(stamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _doing(inputs: TechLeadPageInputs) -> list[TechLeadDoingItemPayload]:
    since = inputs.now - DOING_WINDOW
    rows: list[TechLeadDoingItemPayload] = []
    for decision in inputs.decisions:
        approved = decision.lifecycle in (
            CharterProposalLifecycle.APPROVED_APPLIED,
            CharterProposalLifecycle.APPROVED_STALE,
        )
        if decision.outcome is not CharterOutcome.EXECUTED and not approved:
            continue
        in_flight = decision.outcome is CharterOutcome.EXECUTED and decision.execution is None
        at = _parse(decision.effect_at)
        if not in_flight and (at is None or at < since):
            continue
        target = decision.target_number or 0
        rows.append(
            TechLeadDoingItemPayload(
                decision_id=decision.decision_id,
                action_kind=decision.action_kind,
                action_label=decision.action_kind.replace("_", " "),
                target_number=target,
                link=issue_link(inputs.repository, target) if target else "",
                outcome="in_flight" if in_flight else decision.effect,
                outcome_label="In flight" if in_flight else decision.effect.replace("_", " ").capitalize(),
                at=decision.effect_at,
                reason=decision.execution_reason or decision.reason,
                in_flight=in_flight,
            )
        )
    open_proposals = {issue.number for issue, _ in inputs.proposals if issue.state == "open"}
    for receipt in inputs.rework_receipts:
        if receipt.proposal_issue_number in open_proposals:
            continue  # shown on its proposal card
        rows.append(
            TechLeadDoingItemPayload(
                decision_id=f"rework:{receipt.request_key}",
                action_kind="request_rework",
                action_label="request rework",
                target_number=receipt.issue_number,
                link=issue_link(inputs.repository, receipt.issue_number),
                outcome=receipt.status,
                outcome_label=receipt.status.replace("_", " ").capitalize(),
                at=receipt.at,
                reason=receipt.detail,
                in_flight=receipt.status in REWORK_IN_FLIGHT,
            )
        )
    return sorted(rows, key=lambda row: (not row.in_flight, row.at))


def _parked(repository: str, row: "LivenessRow") -> TechLeadParkedActionPayload:
    issue = row.key.escalation_issue or 0
    return TechLeadParkedActionPayload(
        action=row.key.identity.action,
        subject=row.key.identity.subject,
        issue_number=issue,
        outcome=row.last_outcome.value,
        reason=row.last_reason,
        parked_since=row.last_failed_at.isoformat(),
        escalated=row.escalated,
        link=issue_link(repository, issue) if issue else "",
    )


# -- watching -----------------------------------------------------------------


def _triaged(inputs: TechLeadPageInputs) -> list[TechLeadTriagedItemPayload]:
    latest: dict[int, TechLeadCharterDecision] = {}
    for decision in inputs.decisions:
        number = decision.target_number
        if decision.triage_class is None or number is None or number not in inputs.blocked_numbers:
            continue
        held = latest.get(number)
        if held is None or decision.decided_at > held.decided_at:
            latest[number] = decision
    return [
        TechLeadTriagedItemPayload(
            issue_number=number,
            triage_class=decision.triage_class.value,
            triage_label=_TRIAGE_LABELS.get(decision.triage_class.value, decision.triage_class.value),
            decided_at=decision.decided_at,
            reason=decision.reason,
            link=issue_link(inputs.repository, number),
        )
        for number, decision in sorted(latest.items())
        if decision.triage_class is not None
    ]


def _health_review(inputs: TechLeadPageInputs) -> TechLeadHealthReviewPayload:
    interval = inputs.health_interval_minutes
    if interval <= 0:
        return TechLeadHealthReviewPayload(
            enabled=False, interval_minutes=0, last_at="", next_due_at="",
            label="Periodic health reviews are off",
        )
    if inputs.last_health_review_at <= 0:
        return TechLeadHealthReviewPayload(
            enabled=True, interval_minutes=interval, last_at="", next_due_at="",
            label=f"Every {interval} min; none has run yet, so the next is due now",
        )
    last = datetime.fromtimestamp(inputs.last_health_review_at, tz=timezone.utc)
    due = last + timedelta(minutes=interval)
    return TechLeadHealthReviewPayload(
        enabled=True,
        interval_minutes=interval,
        last_at=last.isoformat(),
        next_due_at=due.isoformat(),
        label=f"Every {interval} min; next due {due.isoformat(timespec='minutes')}",
    )


def _run_strip(entry: "TechLeadRunActivityEntry | None") -> TechLeadRunStripPayload:
    if entry is None:
        return TechLeadRunStripPayload(
            has_run=False, label="No tech-lead run recorded yet", phase="", phase_label="",
            started_at="", ended_at="", detail="",
        )
    subject = f"{entry.flavor_label} — {entry.subject_label}"
    return TechLeadRunStripPayload(
        has_run=True,
        label=subject,
        phase=entry.phase,
        phase_label=entry.phase_label,
        started_at=entry.started_at,
        ended_at=entry.ended_at,
        detail=entry.detail,
    )


__all__ = [
    "DOING_WINDOW",
    "ReworkReceiptView",
    "MERGE_HOLD_CAUSES",
    "TechLeadPageInputs",
    "build_tech_lead_page_section",
    "issue_link",
]
