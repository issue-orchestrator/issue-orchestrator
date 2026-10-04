"""Act-level tech-lead op commands (ADR-0031 §2, #6764/#6778/#7399).

Each is the orchestrator's typed command for one act-level ``action_type`` a
tech lead may propose: planned directly under ``execute`` authority or from an
approved gated proposal, and applied by an owner that re-validates the op's
preconditions at apply time. Split out of ``tech_lead_actions`` (which
re-exports every name here) as the op vocabulary grew; like it, this module
depends only on ``action_base``, never on ``actions``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from ..domain.block_resolution import BlockResolution
from ..domain.scoped_rework import ReworkRequest
from ..domain.tech_lead_session import OperatorDecision
from ..domain.validated_work_commands import ValidatedWorkAuthoritySnapshot
from .action_base import Action, ActionType


@dataclass(frozen=True)
class ResetRetryIssueAction(Action):
    """Execute a tech_lead ``reset_retry`` proposal via the reset owner (#6764).

    Planned by ``plan_tech_lead_decision_actions`` ONLY when
    ``tech_lead.authority.reset_retry`` is ``execute``. Proposals are
    stale-checkable facts, not commands (ADR-0031 §2): the applier's owner
    re-validates the recorded preconditions against current state at
    execution time and downgrades to a surfaced proposal
    (``TECH_LEAD_ACTION_PROPOSED``, ``mode="stale_downgrade"``) when the board
    has moved — no mutations are posted on the downgrade path.

    ``anchor_issue_number`` is the tech_lead session's anchor issue — the event
    surface a downgrade is reported against, mirroring
    :class:`SurfaceTechLeadProposalAction`. For failure investigations and
    health reviews the immutable launch scope forces
    ``issue_number == anchor_issue_number``.
    """

    #: The decision's ``action_type`` / stored op this command executes.
    op_type: ClassVar[str] = "reset_retry"

    issue_number: int = 0  # The issue to scratch-reset (the proposal's target)
    rationale: str = ""  # The agent's recorded rationale (proposal body)
    proposal_id: str = ""  # The decision artifact action id (A<n>)
    finding_ids: tuple[str, ...] = ()
    anchor_issue_number: int = 0
    # Set (>0) when this execution consumes an APPROVED gated proposal's
    # stored op (#6778): the applier then finalizes the proposal issue
    # (outcome comment + close + discard_op). 0 = direct execute-authority.
    proposal_issue_number: int = 0
    requires_effective_disposition: bool = False
    action_type: ActionType = field(default=ActionType.RESET_RETRY_ISSUE, init=False)

    def __post_init__(self) -> None:
        if self.issue_number <= 0:
            raise ValueError("ResetRetryIssueAction requires a positive issue_number")
        if not self.proposal_id:
            raise ValueError("ResetRetryIssueAction requires the proposal id")

    def reconciliation_subject(self) -> int:
        """The issue this reset mutates."""
        return self.issue_number


@dataclass(frozen=True)
class KillHungSessionAction(Action):
    """Execute a ``kill_hung_session`` proposal op.

    Planned directly when authority is ``execute`` or from an approved gated
    proposal under ``propose`` (#6778). The applier's owner re-validates that
    the exact target session generation is still active and applies the
    issue-runtime termination boundary — the same ``terminate_issue_runtime``
    the reset owner uses, WITHOUT the reset. Stale proposals downgrade with no
    mutations, mirroring ``reset_retry``.
    """

    #: The decision's ``action_type`` / stored op this command executes.
    op_type: ClassVar[str] = "kill_hung_session"

    issue_number: int = 0  # The issue whose runtime is terminated (op target)
    rationale: str = ""  # The agent's recorded rationale (stored op)
    proposal_id: str = ""  # The decision artifact action id (A<n>)
    finding_ids: tuple[str, ...] = ()
    anchor_issue_number: int = 0  # Event surface: source anchor or gated issue
    # Set (>0) when consuming an approved gated proposal. 0 means direct
    # execute-authority and requires no proposal finalization.
    proposal_issue_number: int = 0
    # The complete typed generation observed at tech-lead launch. The reusable
    # terminal slot, task kind, and per-launch run id travel together into the
    # atomic termination owner; partial/legacy identities fail closed.
    target_session_id: str = ""
    target_terminal_id: str = ""
    target_session_type: str = ""
    requires_effective_disposition: bool = False
    action_type: ActionType = field(default=ActionType.KILL_HUNG_SESSION, init=False)

    def __post_init__(self) -> None:
        if self.issue_number <= 0:
            raise ValueError("KillHungSessionAction requires a positive issue_number")
        if not self.proposal_id:
            raise ValueError("KillHungSessionAction requires the proposal id")
        if self.proposal_issue_number < 0:
            raise ValueError("proposal_issue_number cannot be negative")

    def reconciliation_subject(self) -> int:
        """The issue whose runtime this termination mutates."""
        return self.issue_number


@dataclass(frozen=True)
class RequestReworkAction(Action):
    """A consent-bound instruction to the branch-preserving rework owner."""

    #: The decision's ``action_type`` / stored op this command executes.
    op_type: ClassVar[str] = "request_rework"

    request: ReworkRequest = field(kw_only=True)
    proposal_id: str = field(kw_only=True)
    finding_ids: tuple[str, ...] = ()
    anchor_issue_number: int = 0
    proposal_issue_number: int = 0
    action_type: ActionType = field(default=ActionType.REQUEST_REWORK, init=False)

    @property
    def issue_number(self) -> int:
        return self.request.target.issue_number

    def reconciliation_subject(self) -> int:
        return self.issue_number


@dataclass(frozen=True)
class RecoverValidatedWorkAction(Action):
    """Publish one exact retained validated head through the recovery owner."""

    #: The decision's ``action_type`` / stored op this command executes.
    op_type: ClassVar[str] = "recover_validated_work"

    authority: ValidatedWorkAuthoritySnapshot = field(kw_only=True)
    rationale: str = ""
    proposal_id: str = ""
    finding_ids: tuple[str, ...] = ()
    anchor_issue_number: int = 0
    proposal_issue_number: int = 0
    requires_effective_disposition: bool = False
    action_type: ActionType = field(
        default=ActionType.RECOVER_VALIDATED_WORK, init=False
    )

    def __post_init__(self) -> None:
        if not self.proposal_id:
            raise ValueError("RecoverValidatedWorkAction requires the proposal id")
        if self.proposal_issue_number < 0:
            raise ValueError("proposal_issue_number cannot be negative")

    @property
    def issue_number(self) -> int:
        return self.authority.issue_number

    def reconciliation_subject(self) -> int:
        return self.issue_number


@dataclass(frozen=True)
class ReleaseWithheldReviewAction(Action):
    """Release the review of an issue's open PR withheld only by the issue's block.

    Planned directly under ``execute`` authority or from an approved gated
    proposal (#7399). It carries only intent and provenance: which issue, and
    when the proposing tech lead observed it. The applier's owner
    (``tech_lead_review_release``) re-verifies every precondition against the
    live owners before any write and refuses, typed, when one fails.
    """

    #: The decision's ``action_type`` / stored op this command executes.
    op_type: ClassVar[str] = "release_withheld_review"

    issue_number: int = 0
    rationale: str = ""
    proposal_id: str = ""
    finding_ids: tuple[str, ...] = ()
    anchor_issue_number: int = 0
    proposal_issue_number: int = 0
    #: ISO-8601 instant the proposing tech lead observed the board. A failure
    #: recorded for the issue since then is newer than the block it diagnosed.
    observed_at: str = ""
    #: The proposing run's session name. With ``observed_at`` (its start) it
    #: identifies that run's own claim, the only claim that does not refuse.
    source_session_name: str = ""
    requires_effective_disposition: bool = False
    action_type: ActionType = field(
        default=ActionType.RELEASE_WITHHELD_REVIEW, init=False
    )

    def __post_init__(self) -> None:
        if self.issue_number <= 0:
            raise ValueError("ReleaseWithheldReviewAction requires a positive issue_number")
        if not self.proposal_id:
            raise ValueError("ReleaseWithheldReviewAction requires the proposal id")
        if not self.observed_at:
            raise ValueError("ReleaseWithheldReviewAction requires the observation instant")
        if not self.source_session_name:
            raise ValueError("ReleaseWithheldReviewAction requires the proposing session")
        if self.proposal_issue_number < 0:
            raise ValueError("proposal_issue_number cannot be negative")

    def reconciliation_subject(self) -> int:
        return self.issue_number


@dataclass(frozen=True)
class ApplyOperatorDecisionAction(Action):
    """Carry out a ``propose_decision`` the operator approved (#7593).

    Only ever built from an approved gated proposal: the decision is the
    operator's, so there is no direct-execute path. Its owner
    (``tech_lead_operator_decision``) files the drafted follow-up issues
    create-once, posts the decision on the item, and retries the item through
    the operator's own retry command.
    """

    #: The decision's ``action_type`` / stored op this command executes.
    op_type: ClassVar[str] = "propose_decision"

    issue_number: int = 0
    decision: OperatorDecision = field(kw_only=True)
    proposal_id: str = ""
    finding_ids: tuple[str, ...] = ()
    anchor_issue_number: int = 0
    proposal_issue_number: int = 0
    requires_effective_disposition: bool = False
    action_type: ActionType = field(
        default=ActionType.APPLY_OPERATOR_DECISION, init=False
    )

    def __post_init__(self) -> None:
        if self.issue_number <= 0:
            raise ValueError("ApplyOperatorDecisionAction requires a positive issue_number")
        if not self.proposal_id:
            raise ValueError("ApplyOperatorDecisionAction requires the proposal id")
        if self.proposal_issue_number <= 0:
            raise ValueError(
                "ApplyOperatorDecisionAction runs only from an approved proposal:"
                " the decision is the operator's"
            )

    def reconciliation_subject(self) -> int:
        return self.issue_number


@dataclass(frozen=True)
class ResolveBlockAction(Action):
    """Decide a ``needs-human`` work block in the operator's stead (#7658).

    Planned directly under ``execute`` authority or from an approved gated
    proposal. It carries the decision and its provenance only; the applier's
    owner (``tech_lead_block_resolution``) re-verifies at apply time that the
    causes it names are still recorded, resolvable and never resolved before,
    that the item names no human-only work, and that nothing runs or claims
    it, then posts the decision, files a split's children and discharges only
    those causes through the shared block's owner.
    """

    #: The decision's ``action_type`` / stored op this command executes.
    op_type: ClassVar[str] = "resolve_block"

    issue_number: int = 0
    resolution: BlockResolution = field(kw_only=True)
    rationale: str = ""
    proposal_id: str = ""
    finding_ids: tuple[str, ...] = ()
    anchor_issue_number: int = 0
    proposal_issue_number: int = 0
    #: ISO-8601 instant the proposing tech lead observed the board: a failure
    #: recorded for the item since then is newer than the block it decided.
    observed_at: str = ""
    #: The proposing run: its session (whose own claim does not refuse) and
    #: its run id (with ``proposal_id``, the decision's durable identity).
    source_session_name: str = ""
    source_run_id: str = ""
    #: A split's children are filed as proposals awaiting approval when the
    #: tech lead may not file issues unattended (``create_issue`` authority):
    #: the decision is the tech lead's, each new issue is still approvable. An
    #: approved resolution's children are the operator's own, never gated.
    children_gated: bool = False
    requires_effective_disposition: bool = False
    action_type: ActionType = field(default=ActionType.RESOLVE_BLOCK, init=False)

    def __post_init__(self) -> None:
        if self.issue_number <= 0:
            raise ValueError("ResolveBlockAction requires a positive issue_number")
        if not self.proposal_id:
            raise ValueError("ResolveBlockAction requires the proposal id")
        for name in ("observed_at", "source_session_name", "source_run_id"):
            if not getattr(self, name):
                raise ValueError(f"ResolveBlockAction requires {name}")
        if self.proposal_issue_number < 0:
            raise ValueError("proposal_issue_number cannot be negative")

    @property
    def decision_id(self) -> str:
        """The decision's durable identity, for its markers on GitHub."""
        return f"{self.source_run_id}/{self.proposal_id}"

    def reconciliation_subject(self) -> int:
        return self.issue_number


#: Act-level ops that carry ``requires_effective_disposition``: when one is a
#: failure investigation's terminal remedy, a refused (stale) apply satisfies the
#: investigation only if its result says the remedy's goal already holds.
#: ``request_rework`` is judged by its own receipt instead.
EFFECTIVE_DISPOSITION_OP_ACTIONS = (
    ResetRetryIssueAction,
    KillHungSessionAction,
    RecoverValidatedWorkAction,
    ReleaseWithheldReviewAction,
    ResolveBlockAction,
)
