"""Startup migration from the legacy ``proposed-tech-lead`` gate (#7763)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from issue_orchestrator.control.tech_lead_approval_migration import (
    ApprovalMigrationError,
    migrate_legacy_proposals,
)
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.control.tech_lead_approval_scope import discover_open_gated_proposals
from issue_orchestrator.ports.approval_evidence import InMemoryProposalIssueIndex
from issue_orchestrator.domain.tech_lead_approval import (
    ApprovalVerdictKind,
    ProposalLabelState,
    carries_proposal_marker,
    proposal_label_state,
    proposal_state,
)
from issue_orchestrator.domain.tech_lead_session import StoredTechLeadOp
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore
from tests.approval_helpers import BOT, CONTRIBUTOR, MAINTAINER, FakeApprovalEvidence, make_approvals

_NO_SCOPE = SimpleNamespace(filtering=SimpleNamespace(label=None))

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

    def strip_all_but(self, number: int, labels: list[str]) -> None:
        """A bulk edit outside the engine: leaves only *labels*."""
        self._set(number, labels)

    def _set(self, number: int, labels: list[str]) -> None:
        old = self.issues[number]
        self.issues[number] = Issue(
            number=number, title=old.title, labels=labels, state=old.state, repo="o/r", body=old.body
        )

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
        self.evidence.engine_write(number, label)

    def remove_label(self, number: int, label: str) -> None:
        self.writes.append(("remove", number, label))
        self._set(number, [l for l in self.issues[number].labels if l != label])

    def add_comment(self, number: int, body: str) -> str:
        self.comments.append((number, body))
        return "url"

    def update_issue_body(self, number: int, body: str) -> None:
        self.writes.append(("body", number, "marked" if carries_proposal_marker(body) else "unmarked"))
        old = self.issues[number]
        self.issues[number] = Issue(
            number=number, title=old.title, labels=list(old.labels), state=old.state, repo="o/r", body=body
        )


def _issue(number: int, labels, state: str = "open") -> Issue:
    return Issue(number=number, title=f"#{number}", labels=list(labels), state=state, repo="o/r")


def test_a_still_gated_legacy_proposal_moves_to_the_new_labels() -> None:
    evidence = FakeApprovalEvidence()
    repo = _Repo(evidence, [_issue(443, ["agent:tech-lead", LEGACY])])
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=443, op=_op())

    report = migrate_legacy_proposals(repo, make_approvals(evidence), ops, filtering_label=None)

    assert report.regated == (443,)
    labels = repo.issues[443].labels
    assert LEGACY not in labels
    assert proposal_label_state(labels) is ProposalLabelState.AWAITING
    # New labels go on BEFORE the legacy one comes off: a crash between the
    # writes leaves the item gated.
    assert [w for w in repo.writes] == [
        ("body", 443, "marked"),
        ("add", 443, "tech-lead-proposal"),
        ("add", 443, "awaiting-approval"),
        ("remove", 443, LEGACY),
    ]
    # A later bulk edit that strips every approval label leaves it a proposal.
    stripped = repo.issues[443]
    assert proposal_state(["agent:tech-lead"], stripped.body) is ProposalLabelState.AWAITING


def test_a_legacy_follow_up_without_an_op_moves_too() -> None:
    evidence = FakeApprovalEvidence()
    repo = _Repo(evidence, [_issue(445, ["agent:backend", LEGACY])])

    migrate_legacy_proposals(repo, make_approvals(evidence), InMemoryTechLeadAuthorityStore(), filtering_label=None)

    assert proposal_label_state(repo.issues[445].labels) is ProposalLabelState.AWAITING


def test_the_migration_is_idempotent() -> None:
    evidence = FakeApprovalEvidence()
    repo = _Repo(evidence, [_issue(443, ["agent:tech-lead", LEGACY])])
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=443, op=_op())
    approvals = make_approvals(evidence)
    migrate_legacy_proposals(repo, approvals, ops, filtering_label=None)
    writes = list(repo.writes)

    report = migrate_legacy_proposals(repo, approvals, ops, filtering_label=None)

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

    report = migrate_legacy_proposals(repo, approvals, ops, filtering_label=None)

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

    report = migrate_legacy_proposals(repo, make_approvals(evidence), ops, filtering_label=None)

    assert report.legacy_unapproved == (444,)
    assert proposal_label_state(repo.issues[444].labels) is ProposalLabelState.AWAITING


def test_closed_proposals_are_left_alone() -> None:
    """Executed or declined under the old model: nothing to migrate."""
    evidence = FakeApprovalEvidence()
    evidence.label(443, LEGACY, by=MAINTAINER, removed=True)
    repo = _Repo(evidence, [_issue(443, ["agent:tech-lead"], state="closed"), _issue(9, [LEGACY], state="closed")])
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=443, op=_op())

    report = migrate_legacy_proposals(repo, make_approvals(evidence), ops, filtering_label=None)

    assert not report.changed
    assert repo.writes == []


def test_a_failed_item_fails_the_startup_after_migrating_the_rest() -> None:
    """Fail fast: the legacy label no longer blocks anything, so an
    unmigrated proposal would be schedulable."""
    evidence = FakeApprovalEvidence()
    repo = _Repo(evidence, [_issue(1, [LEGACY]), _issue(2, [LEGACY])])
    repo.fail_on = {1}

    with pytest.raises(ApprovalMigrationError, match="#1"):
        migrate_legacy_proposals(repo, make_approvals(evidence), InMemoryTechLeadAuthorityStore(), filtering_label=None)

    assert proposal_label_state(repo.issues[2].labels) is ProposalLabelState.AWAITING


def test_a_crash_mid_carry_over_resumes_on_the_next_startup() -> None:
    """#7763 review F4: the approval half is written before provenance, so a
    crash right after it leaves an issue the next startup still migrates —
    and the maintainer's old-model approval is not lost."""
    from issue_orchestrator.control.tech_lead_approval import TechLeadApprovals
    from issue_orchestrator.ports.approval_evidence import InMemoryOperatorApprovalRecords

    evidence = FakeApprovalEvidence()
    evidence.label(444, LEGACY, by=MAINTAINER, removed=True)
    repo = _Repo(evidence, [_issue(444, ["agent:tech-lead"])])
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=444, op=_op())
    durable = InMemoryOperatorApprovalRecords()  # the authority store outlives the engine
    original_add = repo.add_label

    def crash_on_provenance(number: int, label: str) -> None:
        if label == "tech-lead-proposal":
            raise RuntimeError("engine killed mid-migration")
        original_add(number, label)

    repo.add_label = crash_on_provenance
    index = InMemoryProposalIssueIndex()  # durable too
    with pytest.raises(ApprovalMigrationError):
        migrate_legacy_proposals(repo, TechLeadApprovals(evidence, durable, index), ops, filtering_label=None)
    assert "approved" in repo.issues[444].labels  # half-done
    assert "tech-lead-proposal" not in repo.issues[444].labels  # outside every label query

    repo.add_label = original_add
    restarted = TechLeadApprovals(evidence, durable, index)
    migrate_legacy_proposals(repo, restarted, ops, filtering_label=None)

    # The restarted migration itself restores the gate (#7763 review r6 F1):
    # the proposal is back in the labelled approval scope, and its carried
    # approval still verifies.
    issue = repo.issues[444]
    assert {"tech-lead-proposal", "awaiting-approval", "approved"} <= set(issue.labels)
    found, retired = discover_open_gated_proposals(repo, _NO_SCOPE)
    assert [i.number for i in found] == [444] and retired == ()
    assert restarted.verify(issue, fresh=True).kind is ApprovalVerdictKind.CONTROL_CENTER


def test_proposals_on_the_new_labels_without_a_marker_are_marked() -> None:
    """#7763 review r2 F1: a partial migration from this version is repaired."""
    evidence = FakeApprovalEvidence()
    repo = _Repo(evidence, [_issue(500, ["tech-lead-proposal", "awaiting-approval"])])

    migrate_legacy_proposals(repo, make_approvals(evidence), InMemoryTechLeadAuthorityStore(), filtering_label=None)

    assert carries_proposal_marker(repo.issues[500].body)


def test_a_carried_over_approval_marks_the_body_first() -> None:
    evidence = FakeApprovalEvidence()
    evidence.label(444, LEGACY, by=MAINTAINER, removed=True)
    repo = _Repo(evidence, [_issue(444, ["agent:tech-lead"])])
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=444, op=_op())

    migrate_legacy_proposals(repo, make_approvals(evidence), ops, filtering_label=None)

    # Approval bound first, then the marker, then provenance (resumable).
    assert [w[0] for w in repo.writes] == ["add", "body", "add"]
    assert repo.writes[1] == ("body", 444, "marked")


def test_an_engine_only_migrates_its_own_scope() -> None:
    """An engine scoped to a label (an e2e run on a shared repository) never
    touches another engine's proposals."""
    evidence = FakeApprovalEvidence()
    repo = _Repo(evidence, [_issue(1, [LEGACY, "run-a"]), _issue(2, [LEGACY, "run-b"])])

    report = migrate_legacy_proposals(
        repo, make_approvals(evidence), InMemoryTechLeadAuthorityStore(), filtering_label="run-a"
    )

    assert report.regated == (1,)
    assert LEGACY in repo.issues[2].labels


def test_an_out_of_scope_op_backed_proposal_is_never_touched() -> None:
    """#7763 review r4 F2: the op-ledger path honours the scope too."""
    evidence = FakeApprovalEvidence()
    evidence.label(444, LEGACY, by=MAINTAINER, removed=True)
    repo = _Repo(evidence, [_issue(444, ["agent:tech-lead", "run-b"])])
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=444, op=_op())
    approvals = make_approvals(evidence)

    report = migrate_legacy_proposals(repo, approvals, ops, filtering_label="run-a")

    assert not report.changed
    assert repo.writes == [] and repo.comments == []
    assert approvals.records.load_operator_approval(444) is None


def test_an_approval_revoked_after_migration_is_never_carried_again() -> None:
    """#7763 review r5 F1: the marker records that migration happened, so a
    later strip of the new labels re-gates the proposal on restart."""
    evidence = FakeApprovalEvidence()
    evidence.label(444, LEGACY, by=MAINTAINER, removed=True)
    repo = _Repo(evidence, [_issue(444, ["agent:tech-lead"])])
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=444, op=_op())
    migrate_legacy_proposals(repo, make_approvals(evidence), ops, filtering_label=None)
    repo.strip_all_but(444, ["agent:tech-lead"])  # someone strips every approval label
    writes = list(repo.writes)

    report = migrate_legacy_proposals(repo, make_approvals(evidence), ops, filtering_label=None)

    # Nothing re-carried: only the gate comes back, never `approved`.
    assert report.legacy_approved == ()
    assert repo.writes[len(writes):] == [
        ("add", 444, "tech-lead-proposal"), ("add", 444, "awaiting-approval"),
    ]
    issue = repo.issues[444]
    assert proposal_state(issue.labels, issue.body) is ProposalLabelState.AWAITING
    assert not make_approvals(evidence).verify(issue, fresh=True).approved


def test_the_migration_seeds_the_proposal_index() -> None:
    """#7763 review r6 F2: every proposal migration touches is indexed."""
    evidence = FakeApprovalEvidence()
    evidence.label(444, LEGACY, by=MAINTAINER, removed=True)
    repo = _Repo(evidence, [
        _issue(1, [LEGACY]),
        _issue(500, ["tech-lead-proposal", "awaiting-approval"]),
        _issue(444, ["agent:tech-lead"]),
    ])
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=444, op=_op())
    approvals = make_approvals(evidence)

    migrate_legacy_proposals(repo, approvals, ops, filtering_label=None)

    assert approvals.indexed_proposals() == {1, 444, 500}


def test_a_proposal_filed_but_never_indexed_is_recovered_at_startup() -> None:
    """#7763 review r7 F3: the engine died between GitHub's create and the
    index write, then a bulk edit stripped every gate label. The startup
    marker sweep indexes it, so the approval scope finds and restores it."""
    from issue_orchestrator.control.tech_lead_approval import plan_approval_settlements
    from issue_orchestrator.domain.tech_lead_approval import ApprovalTransition, with_proposal_marker

    evidence = FakeApprovalEvidence()
    orphan = Issue(number=800, title="t", labels=["agent:backend"], state="open",
                   repo="o/r", body=with_proposal_marker("b"))
    repo = _Repo(evidence, [orphan, _issue(801, ["agent:backend"])])
    approvals = make_approvals(evidence)

    migrate_legacy_proposals(repo, approvals, InMemoryTechLeadAuthorityStore(), filtering_label=None)

    assert approvals.indexed_proposals() == {800}  # never the unmarked #801
    found, _retired = discover_open_gated_proposals(repo, _NO_SCOPE, approvals.indexed_proposals())
    assert [issue.number for issue in found] == [800]
    [settlement] = plan_approval_settlements(found, {}, op_backed=set())
    assert settlement.transition is ApprovalTransition.RESTORE_WAITING
