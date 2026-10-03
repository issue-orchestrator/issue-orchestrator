"""Startup migration from the legacy ``proposed-tech-lead`` gate (#7763)."""

from __future__ import annotations

import pytest

from issue_orchestrator.control.tech_lead_approval_migration import (
    ApprovalMigrationError,
    migrate_legacy_proposals,
)
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.tech_lead_approval import ApprovalVerdictKind, proposal_label_state, ProposalLabelState
from issue_orchestrator.domain.tech_lead_session import StoredTechLeadOp
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore
from tests.approval_helpers import BOT, CONTRIBUTOR, MAINTAINER, FakeApprovalEvidence, make_approvals

LEGACY = "proposed-tech-lead"


def _op(target: int = 13) -> StoredTechLeadOp:
    return StoredTechLeadOp(
        op_type="reset_retry",
        target_issue_number=target,
        rationale="r",
        source_run_id="run-1",
        source_session_name="issue-99",
        source_action_id="A1",
        created_at="2026-10-03T00:00:00+00:00",
    )


class _Repo:
    """A tiny GitHub: open/closed issues whose label writes leave events."""

    def __init__(self, evidence: FakeApprovalEvidence, issues: list[Issue]) -> None:
        self.evidence = evidence
        self.issues = {issue.number: issue for issue in issues}
        self.writes: list[tuple[str, int, str]] = []
        self.comments: list[tuple[int, str]] = []
        self.fail_on: set[int] = set()

    def _set(self, number: int, labels: list[str]) -> None:
        old = self.issues[number]
        self.issues[number] = Issue(number=number, title=old.title, labels=labels, state=old.state, repo="o/r")

    def list_issues(self, labels=None, state="open", limit=100, required_stable_ids=None, *, exhaustive=False):
        assert exhaustive, "the migration must read the COMPLETE legacy set"
        wanted = {label.casefold() for label in labels or []}
        return [
            issue for issue in self.issues.values()
            if issue.state == state and wanted <= {label.casefold() for label in issue.labels}
        ]

    def get_issue(self, number: int) -> Issue | None:
        return self.issues.get(number)

    def add_label(self, number: int, label: str) -> None:
        if number in self.fail_on:
            raise RuntimeError("403")
        self.writes.append(("add", number, label))
        self._set(number, [*self.issues[number].labels, label])
        self.evidence.label(number, label, by=BOT)

    def remove_label(self, number: int, label: str) -> None:
        self.writes.append(("remove", number, label))
        self._set(number, [l for l in self.issues[number].labels if l != label])

    def add_comment(self, number: int, body: str) -> str:
        self.comments.append((number, body))
        return "url"


def _issue(number: int, labels, state: str = "open") -> Issue:
    return Issue(number=number, title=f"#{number}", labels=list(labels), state=state, repo="o/r")


def test_a_still_gated_legacy_proposal_moves_to_the_new_labels() -> None:
    evidence = FakeApprovalEvidence()
    repo = _Repo(evidence, [_issue(443, ["agent:tech-lead", LEGACY])])
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=443, op=_op())

    report = migrate_legacy_proposals(repo, make_approvals(evidence), ops)

    assert report.regated == (443,)
    labels = repo.issues[443].labels
    assert LEGACY not in labels
    assert proposal_label_state(labels) is ProposalLabelState.AWAITING
    # New labels go on BEFORE the legacy one comes off: a crash between the
    # writes leaves the item gated.
    assert [w for w in repo.writes] == [
        ("add", 443, "tech-lead-proposal"),
        ("add", 443, "awaiting-approval"),
        ("remove", 443, LEGACY),
    ]


def test_a_legacy_follow_up_without_an_op_moves_too() -> None:
    evidence = FakeApprovalEvidence()
    repo = _Repo(evidence, [_issue(445, ["agent:backend", LEGACY])])

    migrate_legacy_proposals(repo, make_approvals(evidence), InMemoryTechLeadAuthorityStore())

    assert proposal_label_state(repo.issues[445].labels) is ProposalLabelState.AWAITING


def test_the_migration_is_idempotent() -> None:
    evidence = FakeApprovalEvidence()
    repo = _Repo(evidence, [_issue(443, ["agent:tech-lead", LEGACY])])
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=443, op=_op())
    approvals = make_approvals(evidence)
    migrate_legacy_proposals(repo, approvals, ops)
    writes = list(repo.writes)

    report = migrate_legacy_proposals(repo, approvals, ops)

    assert not report.changed
    assert repo.writes == writes


def test_an_old_model_approval_by_a_maintainer_is_carried_over_as_approved() -> None:
    """porchpin #443/#444: approved by REMOVING the legacy gate, not yet
    executed when the new engine starts. A maintainer's removal is honoured:
    the next tick verifies it and executes exactly once."""
    evidence = FakeApprovalEvidence()
    evidence.label(444, LEGACY, by=MAINTAINER, removed=True)
    repo = _Repo(evidence, [_issue(444, ["agent:tech-lead"])])
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=444, op=_op())
    approvals = make_approvals(evidence)

    report = migrate_legacy_proposals(repo, approvals, ops)

    assert report.legacy_approved == (444,)
    issue = repo.issues[444]
    assert approvals.verify(issue, fresh=True).kind is ApprovalVerdictKind.CONTROL_CENTER
    assert MAINTAINER in repo.comments[0][1]


@pytest.mark.parametrize("remover", [BOT, CONTRIBUTOR, None], ids=["bot", "contributor", "no-event"])
def test_an_old_model_removal_by_anyone_else_is_regated(remover) -> None:
    """Escape vector: the old model read ANY removal (a retry's label strip, a
    bot) as approval. Carried over, such a removal re-gates the proposal."""
    evidence = FakeApprovalEvidence()
    if remover is not None:
        evidence.label(444, LEGACY, by=remover, removed=True)
    repo = _Repo(evidence, [_issue(444, ["agent:tech-lead"])])
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=444, op=_op())

    report = migrate_legacy_proposals(repo, make_approvals(evidence), ops)

    assert report.legacy_unapproved == (444,)
    assert proposal_label_state(repo.issues[444].labels) is ProposalLabelState.AWAITING


def test_closed_proposals_are_left_alone() -> None:
    """Executed or declined under the old model: nothing to migrate."""
    evidence = FakeApprovalEvidence()
    evidence.label(443, LEGACY, by=MAINTAINER, removed=True)
    repo = _Repo(evidence, [_issue(443, ["agent:tech-lead"], state="closed"), _issue(9, [LEGACY], state="closed")])
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=443, op=_op())

    report = migrate_legacy_proposals(repo, make_approvals(evidence), ops)

    assert not report.changed
    assert repo.writes == []


def test_a_failed_item_fails_the_startup_after_migrating_the_rest() -> None:
    """Fail fast: the legacy label no longer blocks anything, so an
    unmigrated proposal would be schedulable."""
    evidence = FakeApprovalEvidence()
    repo = _Repo(evidence, [_issue(1, [LEGACY]), _issue(2, [LEGACY])])
    repo.fail_on = {1}

    with pytest.raises(ApprovalMigrationError, match="#1"):
        migrate_legacy_proposals(repo, make_approvals(evidence), InMemoryTechLeadAuthorityStore())

    assert proposal_label_state(repo.issues[2].labels) is ProposalLabelState.AWAITING
