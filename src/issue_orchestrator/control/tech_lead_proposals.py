"""Gated tech_lead proposal issues (#6778, amends ADR-0031 §2).

Consequential tech_lead proposals become **gated GitHub issues** carrying the
approval model's provenance and waiting labels
(:data:`~..domain.tech_lead_approval.GATED_PROPOSAL_LABELS`). A maintainer's
``approved`` label, verified by :mod:`~.tech_lead_approval`, is per-instance
approval (#7763). This module is the single policy owner for the whole gated
lifecycle:

* **Composition** — :func:`build_tech_lead_proposal_issue_action` turns an
  act-level decision proposal under ``propose`` authority into a
  :class:`CreateTechLeadProposalIssueAction` carrying the typed
  :class:`StoredTechLeadOp`. The issue body is human documentation ONLY.
* **Creation boundary** —
  :func:`tech_lead_issue_creation.apply_create_tech_lead_issue` is the shared
  create-issue executor. Proposal creations record the op create-once in the
  orchestrator-owned authority store and link the issue from the tech_lead
  session's anchor. Execution later consumes only the stored op, so editing
  the issue body after creation has zero effect (the tamper boundary).
* **Ledger dedup** — one open proposal per (op, target):
  :func:`build_op_ledger` projects the store's rows; a duplicate proposal
  plans an :class:`AddCommentAction` on the existing proposal issue instead
  of filing a second one (:func:`build_duplicate_proposal_comment`).
* **Approval backlog** — :func:`observe_gated_tech_lead_proposals` answers
  "what is waiting on the operator?" from LABEL truth over the open issues a
  tick already observed, because only act-level proposals leave a ledger row
  (#7014). It is the visibility counterpart to reconciliation below.
* **Reconciliation** — :func:`reconcile_tech_lead_proposals` is the lifecycle
  owner that partitions the fact gatherer's EXHAUSTIVE open-issue scan (#6779
  R2/R4) against the durable ledger in one pass: an op-backed issue whose
  approval the approval owner verified is approved; any other proposal is
  inert; a
  ledger row whose issue is absent from the scan is only a CANDIDATE for
  terminal cleanup (#6779 R7) — the scan can be truncated, so absence alone
  never proves terminality. Reconciliation stays READ-ONLY: it classifies but
  does not mutate the ledger. Anchor classification runs on the remainder so a
  proposal issue can never be mistaken for a batch/health anchor.
* **Terminal cleanup** — :func:`apply_discard_terminal_tech_lead_proposal_ops` is
  the single mutating boundary the applier invokes on a
  :class:`DiscardTerminalTechLeadProposalOpsAction` the planner emitted from the
  absent-candidate fact. It CONFIRMS each candidate with a fresh targeted read
  before discarding, so a paginated scan gap can never delete a live op.
* **Approval planning** — :func:`plan_approved_tech_lead_op_executions` turns
  approved ops into the typed execution actions (``reset_retry`` reuses the
  #6777 executor + stale policy verbatim; ``kill_hung_session`` uses its own
  executor in ``tech_lead_kill_session``).
* **Execution handoff** — :mod:`tech_lead_proposal_execution` rechecks consent,
  posts terminal outcomes, closes the proposal, and discards its op. These
  functions are re-exported here to preserve the proposal lifecycle API while
  keeping mutation sequencing in its own owner.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence

from ..domain.scoped_rework import ReworkRequest, ReworkReceipt
from ..domain.validated_work import RemoteBaselineStatus
from ..domain.tech_lead_approval import (
    GATED_PROPOSAL_LABELS,
    HOW_TO_APPROVE,
    ApprovalVerdict,
    proposal_state,
    with_proposal_marker,
)
from ..domain.tech_lead_session import (
    ApprovedTechLeadOp,
    GatedTechLeadProposal,
    OperatorDecision,
    StoredTechLeadOp,
    TechLeadCreationOrigin,
    TechLeadSessionGeneration,
)
from .actions import (
    Action,
    ActionResult,
    ApplyOperatorDecisionAction,
    CreateTechLeadProposalIssueAction,
    DiscardTerminalTechLeadProposalOpsAction,
    KillHungSessionAction,
    RecoverValidatedWorkAction,
    ReleaseWithheldReviewAction,
    RequestReworkAction,
    ResetRetryIssueAction,
)
from .reconciliation import build_expected_for_mutation
from .tech_lead_charter_lifecycle import link_declined_proposal
from .tech_lead_proposal_execution import (
    execute_approved_tech_lead_op as execute_approved_tech_lead_op,
    finalize_tech_lead_op_execution as finalize_tech_lead_op_execution,
)

if TYPE_CHECKING:
    from ..domain.tech_lead_artifacts import ProposedTechLeadAction
    from ..domain.validated_work_commands import ValidatedWorkAuthoritySnapshot
    from ..infra.config import Config
    from ..ports import RepositoryHost
    from ..ports.issue import Issue
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .reconciliation import ExpectedState

logger = logging.getLogger(__name__)

# Exhaustive open tech-lead-agent scan bound (#6779 R4). Both the per-tick fact
# gatherer and startup recovery page the COMPLETE open set so a backlog of
# gated proposals can never push an older approved op or a batch/health anchor
# past a small window. The value is a runaway backstop, not an expected size:
# the GitHub adapter pages until a short page, capped here so an unbounded
# scan fails loud rather than looping. Realistic open tech-lead-agent issue
# counts (≤2 anchors + a handful of proposals) are orders of magnitude below.
TECH_LEAD_PROPOSAL_SCAN_LIMIT = 2000

_MAX_DECISION_TITLE_CHARS = 200

# Human-facing verbs per op type, used in proposal issue titles/bodies.
# Titles must never contain "Batch Review"/"Tech Lead Review" (the historical
# batch-anchor title heuristic), and classification additionally excludes
# gate-labeled/op-backed issues before that heuristic runs.
_OP_TITLES: dict[str, str] = {
    "request_rework": "scoped PR rework for issue #{target}",
    "reset_retry": "reset & retry issue #{target} from scratch",
    "kill_hung_session": "kill hung session for issue #{target}",
    "recover_validated_work": "recover retained validated work for issue #{target}",
    "release_withheld_review": "release the withheld review of issue #{target}'s PR",
    "propose_decision": "decide for issue #{target}",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def proposal_issue_labels(config: "Config") -> tuple[str, ...]:
    """Labels for a gated act-level proposal issue.

    The tech lead agent label keeps the proposal inside the fact gatherer's ONE
    anchor scan; the filtering label keeps it inside the active scope (the
    anchor classifier ignores out-of-scope issues); the approval model's
    provenance and waiting labels block pickup until a maintainer approves.
    Orchestrator-attached: those labels are exempt here and ONLY here — the
    agent-label allowlist rejects them.
    """
    return tuple(
        value
        for value in (
            config.tech_lead_review_agent,
            config.filtering.label,
            *GATED_PROPOSAL_LABELS,
        )
        if value
    )


def build_stored_tech_lead_op(
    proposed: "ProposedTechLeadAction",
    *,
    source_run_id: str,
    source_session_name: str,
    target_session: TechLeadSessionGeneration | None = None,
    rework_request: ReworkRequest | None = None,
    validated_work_authority: "ValidatedWorkAuthoritySnapshot | None" = None,
    observed_at: str = "",
    now_iso: str | None = None,
) -> StoredTechLeadOp:
    """The orchestrator-side executable payload for an act-level proposal.

    ``target_session`` binds a ``kill_hung_session`` op to the exact trusted
    generation observed at tech-lead launch (#6779 R1); it stays absent for
    ``reset_retry`` (label/no-session stale-checked).
    ``proposed.finding_ids`` are persisted so execution correlates to the
    findings the approver saw (#6779 R6).
    """
    assert proposed.target_number is not None  # enforced by validate()
    return StoredTechLeadOp(
        op_type=proposed.action_type,
        target_issue_number=rework_request.target.issue_number if rework_request else proposed.target_number,
        rework_request=rework_request,
        validated_work_authority=validated_work_authority,
        rationale=proposed.body or "",
        source_run_id=source_run_id,
        source_session_name=source_session_name,
        source_action_id=proposed.id,
        created_at=now_iso or _utc_now_iso(),
        target_session_id=target_session.run_id if target_session else "",
        target_terminal_id=target_session.terminal_id if target_session else "",
        target_session_type=(target_session.task_kind.value if target_session else ""),
        finding_ids=tuple(proposed.finding_ids),
        observed_at=observed_at,
        decision=operator_decision_of(proposed),
    )


def operator_decision_of(proposed: "ProposedTechLeadAction") -> OperatorDecision | None:
    """The decision a ``propose_decision`` puts to the operator, else None."""
    if proposed.action_type != "propose_decision":
        return None
    return OperatorDecision(
        title=proposed.title or "", body=proposed.body or "", follow_ups=proposed.follow_up_issues,
    )


def _decision_section(op: StoredTechLeadOp) -> str:
    """What approving a ``propose_decision`` does, in the operator's words (#7593)."""
    assert op.decision is not None
    follow_ups = "".join(
        f"\n#### Follow-up {index}: {item.title}\n\n{item.body}\n"
        for index, item in enumerate(op.decision.follow_ups, start=1)
    )
    filed = (
        f"\n### Issues approval files\n{follow_ups}"
        if op.decision.follow_ups
        else "\nApproval files no new issue.\n"
    )
    return f"""## Decision for #{op.target_issue_number}: {op.decision.title}

{op.decision.body}
{filed}
### What approving does

1. Files the issues above, if any, with #{op.target_issue_number}'s own labels
   and milestone.
2. Posts this decision on #{op.target_issue_number}, so the session that resumes
   it works to it.
3. Retries #{op.target_issue_number} last, through the operator's own retry (its
   blocking labels come off). If the item closed or is no longer blocked, or a
   cause the retry may not override (such as a claim quarantine) holds it, the
   item is not retried and this proposal closes saying so.

"""


def _proposal_issue_body(
    op: StoredTechLeadOp, *, anchor_issue_number: int, finding_ids: Sequence[str]
) -> str:
    findings = ", ".join(finding_ids) or "none"
    # kill_hung_session binds approval to one live session generation (#6779
    # R1): show the run id the operator is consenting to terminate so an
    # execution that no-ops on a replacement is auditable against this body.
    session_row = (
        f"| Target session | `{op.target_session_type}` terminal"
        f" `{op.target_terminal_id}`, run `{op.target_session_id}`"
        " (approval kills only this generation) |\n"
        if op.op_type == "kill_hung_session"
        else ""
    )
    if op.op_type == "release_withheld_review":
        session_row += (
            f"| Observed | {op.observed_at} (a failure recorded after this refuses the release) |\n"
            "| Predicted effects | pr-pending on, the PR's review label kept, then only"
            " blocked-failed off; re-verified at approval, refused typed if stale |\n"
        )
    if op.rework_request is not None:
        request = op.rework_request
        target = request.target
        session_row += (
            f"| PR | {target.repository}#{target.pr_number} |\n"
            f"| Expected head | `{target.head_sha}` |\n"
            f"| Evidence | `{request.evidence_identity}` |\n"
            f"| Predicted effects | Preserve `{target.branch}`; invalidate review labels; queue normal rework. "
            "Clear only the observed operator human block; independent causes stay. A merged PR creates a forward fix. |\n"
        )
    if op.validated_work_authority is not None:
        authority = op.validated_work_authority
        observed = authority.remote_baseline_status.value
        if authority.expected_remote_head_sha is not None:
            remote_head = authority.expected_remote_head_sha
        elif (
            authority.remote_baseline_status is RemoteBaselineStatus.OBSERVED
        ):
            remote_head = "absent"
        else:
            remote_head = "unknown"
        session_row += (
            f"| Retained record | `{authority.record_id}` |\n"
            f"| Evidence | `{authority.evidence_id}` revision"
            f" `{authority.observation_revision}` |\n"
            f"| Repository | `{authority.repo_slug}` |\n"
            f"| Validated head | `{authority.validated_head_sha}` on"
            f" `{authority.branch_name}` |\n"
            f"| Remote observation | `{observed}` |\n"
            f"| Approved remote baseline | `{remote_head}`;"
            f" PR `{authority.pr_number if authority.pr_number is not None else 'none'}` |\n"
        )
    decision = _decision_section(op) if op.decision is not None else ""
    return f"""{decision}## Gated tech_lead proposal (ADR-0031 §2)

A tech_lead session proposed an act-level operation. It is **inert** until a
human approves it.

| | |
|---|---|
| Operation | `{op.op_type}` |
| Target | #{op.target_issue_number} |
{session_row}| Proposed by | session `{op.source_session_name}` (run `{op.source_run_id}`, action {op.source_action_id}) |
| Anchor issue | #{anchor_issue_number} |
| Findings | {findings} |

### Rationale

{op.rationale}

### How to approve

{HOW_TO_APPROVE} Once approved, the orchestrator re-validates the operation's
preconditions against current state and executes it exactly once, then closes
this issue with the outcome. If the preconditions no longer hold, it comments
and closes without acting.

> This body is documentation only. The executable payload was recorded
> orchestrator-side when this issue was created; editing this issue has no
> effect on what runs.
"""


def build_tech_lead_proposal_issue_action(
    proposed: "ProposedTechLeadAction",
    *,
    config: "Config",
    anchor_issue_number: int,
    source_run_id: str,
    source_session_name: str,
    expected: "ExpectedState",
    target_session: TechLeadSessionGeneration | None = None,
    rework_request: ReworkRequest | None = None,
    validated_work_authority: "ValidatedWorkAuthoritySnapshot | None" = None,
    observed_at: str = "",
    now_iso: str | None = None,
) -> CreateTechLeadProposalIssueAction:
    """Compose the gated proposal issue creation for an act-level proposal.

    ``target_session`` is the trusted target generation observed at launch,
    bound onto the stored op so kill approval consents to that exact runtime.
    """
    op = build_stored_tech_lead_op(
        proposed,
        source_run_id=source_run_id,
        source_session_name=source_session_name,
        target_session=target_session,
        rework_request=rework_request,
        validated_work_authority=validated_work_authority,
        observed_at=observed_at,
        now_iso=now_iso,
    )
    title_detail = _OP_TITLES[op.op_type].format(target=op.target_issue_number)
    if op.decision is not None:
        # GitHub caps a title at 256 characters; the full decision is in the body.
        title_detail = f"{title_detail}: {op.decision.title}"[:_MAX_DECISION_TITLE_CHARS]
    return CreateTechLeadProposalIssueAction(
        title=f"Tech Lead proposal: {title_detail}",
        body=with_proposal_marker(_proposal_issue_body(
            op,
            anchor_issue_number=anchor_issue_number,
            finding_ids=proposed.finding_ids,
        )),
        labels=proposal_issue_labels(config),
        pr_count=0,
        op=op,
        origin=TechLeadCreationOrigin.derived_from_anchor(anchor_issue_number),
        reason=(
            f"tech_lead decision action {proposed.id}: gated {op.op_type} proposal"
            f" for issue #{op.target_issue_number} (#6778)"
        ),
        expected=expected,
    )


def proposal_ledger_key(
    op_type: str,
    target_issue_number: int,
    *,
    rework_request: ReworkRequest | None = None,
    decision: OperatorDecision | None = None,
) -> tuple[str, int | str]:
    """The identity one open proposal owns: the op and what it would do.

    A scoped rework is keyed by its request; an operator decision by its
    target AND the exact decision (#7593 review F2), because approval executes
    the stored payload: a re-proposal of a DIFFERENT decision for the same item
    must not be recorded as awaiting approval on a proposal that would run the
    old one. Every other op is keyed by its target issue.
    """
    if rework_request is not None:
        return (op_type, rework_request.key)
    if decision is not None:
        digest = hashlib.sha256(
            json.dumps(decision.to_dict(), sort_keys=True).encode()
        ).hexdigest()[:16]
        return (op_type, f"{target_issue_number}:{digest}")
    return (op_type, target_issue_number)


def build_op_ledger(
    ops: Iterable[tuple[int, StoredTechLeadOp]],
    receipts: Iterable[ReworkReceipt] = (),
) -> dict[tuple[str, int | str], int]:
    """Project store rows to a :func:`proposal_ledger_key` -> proposal-issue map.

    The store row lifetime IS the "open proposal" window: rows are created
    with the proposal issue and discarded at terminal handling, so this
    ledger enforces one open proposal per identity without a GitHub read.
    """
    return {("request_rework", receipt.request.key): receipt.proposal_issue_number for receipt in receipts if receipt.proposal_issue_number} | {
        proposal_ledger_key(
            op.op_type, op.target_issue_number,
            rework_request=op.rework_request, decision=op.decision,
        ): issue_number
        for issue_number, op in ops
    }


def build_duplicate_proposal_comment(
    proposed: "ProposedTechLeadAction", *, anchor_issue_number: int
) -> str:
    """Comment for a re-proposal of an already-open (op, target) proposal."""
    return (
        "## 🔁 Proposed again by tech_lead\n\n"
        f"A tech_lead session (anchor #{anchor_issue_number}, action"
        f" {proposed.id}) proposed `{proposed.action_type}` for"
        f" #{proposed.target_number} again. This open proposal already covers"
        f" it. {HOW_TO_APPROVE}\n\n"
        f"### Latest rationale\n\n{proposed.body or ''}"
    )


@dataclass(frozen=True)
class ReconciledTechLeadProposals:
    """Live partition of the EXHAUSTIVE open tech-lead-agent scan vs the ledger.

    The single lifecycle-owner view every caller reads instead of re-deriving
    proposal state from an open-only scan (#6779 R2). A live proposal always
    carries the tech-lead-agent label and is open, so its presence in the scan
    is authoritative: op-backed with a VERIFIED approval -> approved; any other
    proposal -> inert (#7763).

    Absence is NOT authoritative, though (#6779 R7): the exhaustive scan can be
    truncated by a later-page API failure or a >2000-issue repo, dropping a
    still-open proposal from the result. So ``absent_op_issue_numbers`` are
    only CANDIDATES for terminal cleanup — the discard owner
    (:func:`apply_discard_terminal_tech_lead_proposal_ops`) confirms each with a
    fresh targeted read before deleting its ledger row.
    """

    anchor_candidate_issues: list["Issue"]  # -> batch/health anchor classifier
    approved: tuple[ApprovedTechLeadOp, ...]  # verified approval -> execute
    # Ledger rows whose proposal issue was absent from the exhaustive scan:
    # candidates for cleanup, confirmed terminal (deleted/closed) before discard.
    absent_op_issue_numbers: tuple[int, ...]


def reconcile_tech_lead_proposals(
    issues: Sequence["Issue"],
    *,
    ops: Mapping[int, StoredTechLeadOp],
    verdicts: Mapping[int, ApprovalVerdict],
    pending_markers: tuple[str, ...] = (),
) -> ReconciledTechLeadProposals:
    """Classify the exhaustive open scan against the durable ledger.

    ``verdicts`` are the approval owner's answers for the scanned items that
    carry ``approved``. An op is approved only when its verdict approves: a
    stripped label, a retry, or a bot's ``approved`` never executes anything
    (#7763). An op-backed issue is never an anchor, approved or not. Missing
    op-backed issues are cleanup CANDIDATES, since a truncated scan can omit
    live issues; the confirm-and-discard owner re-reads them (#6779 R7).
    Issues attributed to pending creations stay out of anchor classification
    until the creation owner restores their op. Proposals are never anchors.
    """
    open_numbers = {issue.number for issue in issues}
    remaining: list["Issue"] = []
    approved: list[ApprovedTechLeadOp] = []
    for issue in issues:
        op = ops.get(issue.number)
        if op is not None:
            verdict = verdicts.get(issue.number)
            if verdict is not None and verdict.approved:
                approved.append(
                    ApprovedTechLeadOp(proposal_issue_number=issue.number, op=op)
                )
            continue
        if proposal_state(issue.labels, issue.body).is_proposal:
            # A proposal without an op (follow-up, promotion): inert, and its
            # body marker keeps it one even with every gate label stripped
            # (#7763 review r10 F2).
            continue
        # Accepted creates awaiting ledger recovery are never review anchors,
        # even if their labels were edited.
        if not any(marker in (issue.body or "") for marker in pending_markers):
            remaining.append(issue)
    absent = tuple(sorted(number for number in ops if number not in open_numbers))
    return ReconciledTechLeadProposals(
        anchor_candidate_issues=remaining,
        approved=tuple(approved),
        absent_op_issue_numbers=absent,
    )


def observe_gated_tech_lead_proposals(
    *observed: Sequence["Issue"],
) -> tuple[GatedTechLeadProposal, ...]:
    """The approval backlog as LABEL truth: every open, unapproved proposal (#7014).

    The lifecycle owner's answer to "what is waiting on the operator?", and the
    counterpart to :func:`reconcile_tech_lead_proposals`, which classifies the
    same gate against the op ledger. Reconciliation exists to decide what to
    EXECUTE, so it can afford to look only at ledger-backed issues; visibility
    cannot. Act-level op proposals are the only gated issues that leave a
    ``tech_lead_proposal_ops`` row — promoted findings (#6957) and plain
    follow-up issues carry the same gate with no row at all — so a projection
    built from the ledger renders "no open proposals" while a backlog of gated
    issues waits on GitHub. That is what #7014 hid: 20 gated issues, empty
    ledger, and an operator board that said ``None.``

    Callers pass whatever open-issue sets the tick ALREADY holds (the runnable
    board fetch, the exhaustive anchor scan); the union is deduplicated by
    issue number and ordered by it, so the projection is deterministic and
    costs zero GitHub calls. Only OPEN issues qualify: a closed issue is
    rejected or already handled, never awaiting approval.

    Pass EVERY set the tick holds, not just the worker board. That board is
    narrowed by configured agents, milestones, exclusion filters and a fetch
    limit, so it is a runnable-work fetch rather than the approval scope — a
    gated issue outside it is still waiting on an operator. Feeding only the
    board is how this projection could report nothing on a tick that had
    already fetched a pending approval through the anchor scan.
    """
    # One issue can appear in several observed sets; the LAST observation of it
    # wins, so a set gathered later in the tick refreshes an earlier snapshot.
    # Resolve the LATEST observation of each issue FIRST, then judge approval
    # state. Filtering first meant a later observation that does NOT await
    # approval was discarded instead of superseding the earlier one, so an
    # issue approved or closed between two of this tick's fetches stayed
    # advertised as pending despite fresher evidence in the same tick. "Last
    # observation wins" has to include observing that it is no longer waiting.
    latest: dict[int, "Issue"] = {
        issue.number: issue for issues in observed for issue in issues
    }
    backlog: dict[int, GatedTechLeadProposal] = {
        number: _gated_proposal_summary(issue)
        for number, issue in latest.items()
        if _awaits_approval(issue)
    }
    return tuple(backlog[number] for number in sorted(backlog))


def _gated_proposal_summary(issue: "Issue") -> GatedTechLeadProposal:
    """Project one observed issue onto the backlog fact."""
    return GatedTechLeadProposal(
        issue_number=issue.number,
        title=issue.title,
        created_at=issue.created_at or "",
    )


def _awaits_approval(issue: "Issue") -> bool:
    """True iff *issue* is an open proposal no one has approved (#7763)."""
    return issue.state == "open" and proposal_state(issue.labels, issue.body).gate_closed


def _proposal_issue_is_open(tracker: "RepositoryHost", issue_number: int) -> bool:
    """Fresh targeted read: is this proposal issue confirmably still open?

    The exhaustive open scan can be truncated — a later-page API failure, or a
    repo with more open issues than :data:`TECH_LEAD_PROPOSAL_SCAN_LIMIT` — so a
    ledger row's absence from it is only a candidate for cleanup (#6779 R7).
    This re-reads the ONE issue directly: ``open`` means the scan had a gap and
    the op is live; ``closed`` or absent (deleted) means the proposal is
    genuinely terminal. A transient read error raises out of ``get_issue_state``
    and aborts the whole discard action, so a momentary API failure never
    deletes a live op.
    """
    return tracker.get_issue_state(issue_number) == "open"


def apply_discard_terminal_tech_lead_proposal_ops(
    action: Action,
    *,
    tracker: "RepositoryHost | None",
    authority: "TechLeadAuthorityStore | None",
) -> ActionResult:
    """Confirm-and-discard terminal gated-proposal ledger rows (#6779 R7/R10).

    The single mutating boundary for proposal-op cleanup, invoked by the
    applier off the read-only fact path. :func:`reconcile_tech_lead_proposals`
    only CLASSIFIES which ledger rows were absent from the exhaustive scan;
    the planner surfaces those numbers as a
    :class:`DiscardTerminalTechLeadProposalOpsAction`; this owner CONFIRMS each
    candidate with a fresh targeted read before discarding.

    A still-open candidate is a scan gap and its op is PRESERVED (never
    deleted); a closed or deleted candidate is genuinely terminal and its op
    is discarded. Discards are idempotent (``discard_op`` no-ops on an absent
    row), so a candidate confirmed terminal but re-emitted next tick self-heals.
    """
    assert isinstance(action, DiscardTerminalTechLeadProposalOpsAction)
    if tracker is None or authority is None:
        return ActionResult.fail(
            action,
            "terminal tech_lead proposal cleanup requires repository_host and the"
            " TechLeadAuthorityStore wired into this applier",
        )
    discarded: list[int] = []
    preserved: list[int] = []
    for issue_number in action.candidate_issue_numbers:
        if _proposal_issue_is_open(tracker, issue_number):
            preserved.append(issue_number)
            logger.info(
                "[tech_lead] Proposal #%d absent from the open scan but still open:"
                " preserving its ledger op (scan gap, #6779 R7)",
                issue_number,
            )
            continue
        link_declined_proposal(authority, issue_number)
        authority.discard_op(issue_number=issue_number)
        discarded.append(issue_number)
        logger.info(
            "[tech_lead] Confirmed terminal proposal #%d: discarded its leaked"
            " ledger op (#6779 R7/R10)",
            issue_number,
        )
    return ActionResult.ok(
        action,
        discarded_op_count=len(discarded),
        preserved_op_count=len(preserved),
    )


def plan_approved_tech_lead_op_executions(
    approved: Sequence[ApprovedTechLeadOp],
) -> list[Action]:
    """Turn approved stored ops into their typed execution actions.

    The proposal issue is the surface the operator approved on, so it is
    also the event/downgrade anchor for the execution. Precondition
    re-validation (#6777's stale policy for ``reset_retry``; the
    active-session policy for ``kill_hung_session``) happens in the
    executors at apply time — planning stays read-free.
    """
    actions: list[Action] = []
    for item in approved:
        op = item.op
        reason = (
            f"approved tech_lead proposal #{item.proposal_issue_number}:"
            f" execute {op.op_type} for issue"
            f" #{op.target_issue_number} (#6778)"
        )
        # Both executors carry the approved findings so TECH_LEAD_ACTION_EXECUTED
        # correlates back to what the approver saw (#6779 R6). kill also carries
        # the session generation it consented to terminate (#6779 R1); any other
        # act-level op is reset_retry (StoredTechLeadOp validated op_type).
        if op.op_type == "request_rework":
            assert op.rework_request is not None
            actions.append(RequestReworkAction(
                request=op.rework_request, proposal_id=op.source_action_id,
                finding_ids=op.finding_ids, anchor_issue_number=item.proposal_issue_number,
                proposal_issue_number=item.proposal_issue_number, reason=reason,
                expected=build_expected_for_mutation(),
            ))
        elif op.op_type == "recover_validated_work":
            assert op.validated_work_authority is not None
            actions.append(
                RecoverValidatedWorkAction(
                    authority=op.validated_work_authority,
                    rationale=op.rationale,
                    proposal_id=op.source_action_id,
                    finding_ids=op.finding_ids,
                    anchor_issue_number=item.proposal_issue_number,
                    proposal_issue_number=item.proposal_issue_number,
                    reason=reason,
                    expected=build_expected_for_mutation(),
                )
            )
        elif op.op_type == "release_withheld_review":
            actions.append(
                ReleaseWithheldReviewAction(
                    issue_number=op.target_issue_number,
                    rationale=op.rationale,
                    proposal_id=op.source_action_id,
                    finding_ids=op.finding_ids,
                    anchor_issue_number=item.proposal_issue_number,
                    proposal_issue_number=item.proposal_issue_number,
                    observed_at=op.observed_at,
                    source_session_name=op.source_session_name,
                    reason=reason,
                    expected=build_expected_for_mutation(),
                )
            )
        elif op.op_type == "propose_decision":
            assert op.decision is not None
            actions.append(
                ApplyOperatorDecisionAction(
                    issue_number=op.target_issue_number,
                    decision=op.decision,
                    proposal_id=op.source_action_id,
                    finding_ids=op.finding_ids,
                    anchor_issue_number=item.proposal_issue_number,
                    proposal_issue_number=item.proposal_issue_number,
                    reason=reason,
                    expected=build_expected_for_mutation(),
                )
            )
        elif op.op_type == "kill_hung_session":
            actions.append(
                KillHungSessionAction(
                    issue_number=op.target_issue_number,
                    rationale=op.rationale,
                    proposal_id=op.source_action_id,
                    finding_ids=op.finding_ids,
                    anchor_issue_number=item.proposal_issue_number,
                    proposal_issue_number=item.proposal_issue_number,
                    target_session_id=op.target_session_id,
                    target_terminal_id=op.target_terminal_id,
                    target_session_type=op.target_session_type,
                    reason=reason,
                    expected=build_expected_for_mutation(),
                )
            )
        else:
            actions.append(
                ResetRetryIssueAction(
                    issue_number=op.target_issue_number,
                    rationale=op.rationale,
                    proposal_id=op.source_action_id,
                    finding_ids=op.finding_ids,
                    anchor_issue_number=item.proposal_issue_number,
                    proposal_issue_number=item.proposal_issue_number,
                    reason=reason,
                    expected=build_expected_for_mutation(),
                )
            )
        logger.info(
            "Planner: approved tech_lead proposal #%d -> %s for issue #%d",
            item.proposal_issue_number,
            op.op_type,
            op.target_issue_number,
        )
    return actions
