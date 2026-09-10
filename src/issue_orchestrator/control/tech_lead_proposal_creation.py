"""Crash-safe creation through the existing StoredTechLeadOp lifecycle.

The outbox is pre-create authority, not a second approval ledger. A retry uses
its original instruction and authoritative remote marker lookup, including when
GitHub accepted a write whose response was lost. Finalization still belongs to
the normal proposal lifecycle.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable
from uuid import uuid4
from ..domain.tech_lead_proposal_creation import (
    PendingTechLeadProposal,
    ProposalCreationAuthority,
    proposal_creation_key,
)
from ..ports import RepositoryHost
from ..ports.tech_lead_authority import TechLeadAuthorityStore
from .actions import Action, ActionType, ActionResult, CreateTechLeadProposalIssueAction
from ..domain.tech_lead_session import TechLeadCreationOrigin
from .reconciliation import ExpectedState, build_expected_for_mutation
from .scoped_rework_eligibility import rework_target_stale_reason
from .tech_lead_issue_labels import required_label_provisioning_error


@dataclass(frozen=True)
class TechLeadProposalCreation:
    authority: TechLeadAuthorityStore
    repository: RepositoryHost

    def create(
        self,
        action: CreateTechLeadProposalIssueAction,
        milestone: int | None,
        *,
        guard: Callable[[Action, int], None],
    ) -> int:
        key = proposal_creation_key(action.op)
        for number, op in self.authority.list_ops():
            if proposal_creation_key(op) == key:
                self.authority.discard_pending_proposal(key)
                return number
        pending = self.authority.load_pending_proposal(key)
        if pending is None:
            assert action.expected is not None
            pending = PendingTechLeadProposal(
                action.op,
                action.title,
                action.body,
                tuple(action.labels),
                milestone,
                f"<!-- tech-lead-proposal:{uuid4().hex} -->",
                ProposalCreationAuthority(
                    action.anchor_issue_number,
                    tuple(sorted(action.expected.required_labels)),
                    tuple(sorted(action.expected.forbidden_labels)),
                    action.expected.required_pr_state,
                ),
            )
            self.authority.record_pending_proposal(pending)
        return self._resolve(pending, guard=guard)

    def _resolve(
        self,
        pending: PendingTechLeadProposal,
        *,
        guard: Callable[[Action, int], None],
    ) -> int:
        # The port guarantees a complete, title-independent search. An exception
        # is ambiguous and propagates; only its authoritative None permits create.
        number = next(
            (number for number, op in self.authority.list_ops() if op == pending.op),
            None,
        )
        if number is None:
            number = self.repository.find_issue_by_marker(
                title=pending.title,
                marker=pending.marker,
                authoritative=True,
            )
            if number is None:
                number = self._create_missing(pending, guard=guard)
        self._guard(pending, guard=guard, proposal_number=number)
        self._commit(pending, number)
        return number

    def _create_missing(
        self,
        pending: PendingTechLeadProposal,
        *,
        guard: Callable[[Action, int], None],
    ) -> int:
        original = self._original_creation(pending)
        before_write = lambda: self._guard_creation(pending, guard=guard)
        before_write()
        error = required_label_provisioning_error(
            original,
            repository_host=self.repository,
            before_write=before_write,
        )
        if error:
            raise ValueError(error)
        before_write()
        result = self.repository.create_issue(
            title=pending.title,
            body=f"{pending.marker}\n{pending.body}",
            labels=list(pending.labels),
            milestone=pending.milestone,
        )
        number = result.get("number") if result else None
        if type(number) is not int or number <= 0:
            raise RuntimeError(
                "Proposal creation returned no issue identity; durable intent retained"
            )
        return number

    def _guard_creation(
        self,
        pending: PendingTechLeadProposal,
        *,
        guard: Callable[[Action, int], None],
    ) -> None:
        request = pending.op.rework_request
        if request is not None:
            stale = rework_target_stale_reason(
                request,
                self.repository.get_pr(request.target.pr_number),
                self.repository.get_issue(request.target.issue_number),
            )
            if stale:
                raise ValueError(
                    f"Pending proposal target is stale: {stale}; intent retained"
                )
        self._guard(pending, guard=guard)

    @staticmethod
    def _original_creation(
        pending: PendingTechLeadProposal,
    ) -> CreateTechLeadProposalIssueAction:
        authority = pending.creation_authority
        if authority is None:
            raise ValueError(
                "Pending proposal lacks original creation authority; intent retained"
            )
        # Reconstruct only the original capability and gated payload. Do not
        # dispatch the source action or repeat its surrounding session effects.
        return CreateTechLeadProposalIssueAction(
            op=pending.op,
            title=pending.title,
            body=pending.body,
            labels=pending.labels,
            origin=TechLeadCreationOrigin.derived_from_anchor(
                authority.anchor_issue_number
            ),
            expected=ExpectedState(
                frozenset(authority.required_labels),
                frozenset(authority.forbidden_labels),
                authority.required_pr_state,
            ),
        )

    def _guard(
        self,
        pending: PendingTechLeadProposal,
        *,
        guard: Callable[[Action, int], None],
        proposal_number: int | None = None,
    ) -> None:
        if pending.creation_authority is not None:
            original = self._original_creation(pending)
            guard(original, original.anchor_issue_number)
        # Original anchor expectations apply to their original subject only.
        # The target, scoped PR and recovered issue also need fresh pause/claim gates.
        recovery = RecoverTechLeadProposalAction(
            creation_key=pending.key,
            issue_number=pending.op.target_issue_number,
            expected=build_expected_for_mutation(),
        )
        subjects = {pending.op.target_issue_number}
        if pending.op.rework_request is not None:
            subjects.add(pending.op.rework_request.target.pr_number)
        if proposal_number is not None:
            subjects.add(proposal_number)
        for subject in sorted(subjects):
            guard(recovery, subject)

    def _commit(self, pending: PendingTechLeadProposal, number: int) -> None:
        """Both creation and recovery commit only the original stored intent."""
        self.authority.record_op(issue_number=number, op=pending.op)
        self.authority.discard_pending_proposal(pending.key)

    def recover(
        self,
        action: RecoverTechLeadProposalAction,
        *,
        guard: Callable[[Action, int], None],
    ) -> ActionResult:
        """Resume the durable creation transaction, without source-action replay."""
        pending = self.authority.load_pending_proposal(action.creation_key)
        if pending is None:
            return ActionResult.ok(action)
        if pending.op.target_issue_number != action.issue_number:
            return ActionResult.fail(
                action, "Pending proposal target differs from recovery command"
            )
        number = self._resolve(pending, guard=guard)
        return ActionResult.ok(action, issue_number=number, recovered=True)


@dataclass(frozen=True)
class RecoverTechLeadProposalAction(Action):
    """Resume a pending creation using durable attribution and original authority."""

    creation_key: str = ""
    issue_number: int = 0
    action_type: ActionType = field(
        default=ActionType.RECOVER_TECH_LEAD_PROPOSAL, init=False
    )

    def __post_init__(self) -> None:
        if not self.creation_key or self.issue_number <= 0:
            raise ValueError(
                "Proposal recovery requires its creation key and target subject"
            )

    def reconciliation_subject(self) -> int:
        return self.issue_number


def apply_recover_tech_lead_proposal(
    action: Action,
    *,
    authority: TechLeadAuthorityStore | None,
    repository: RepositoryHost | None,
    guard: Callable[[Action, int], None],
) -> ActionResult:
    assert isinstance(action, RecoverTechLeadProposalAction)
    if authority is None or repository is None:
        return ActionResult.fail(
            action, "Proposal recovery requires its authority store and repository"
        )
    return TechLeadProposalCreation(authority, repository).recover(action, guard=guard)
