"""The tech-lead proposal approval owner (#7763).

Approval is a POSITIVE act: a maintainer's ``approved`` label, or Approve in
the Control Center. Each escape vector the old "remove the gate to approve"
model had is pinned here against the owner that closes it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.retry_policy import labels_to_remove_for_retry
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.actions import SettleProposalApprovalAction
from issue_orchestrator.control.scheduler import AvailabilityReason, Scheduler
from issue_orchestrator.control.tech_lead_approval import plan_approval_settlements
from issue_orchestrator.control.tech_lead_approval_writes import (
    apply_operator_proposal_command,
    apply_settle_proposal_approval,
)
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.scoped_rework import TechLeadProposalCommand
from issue_orchestrator.domain.tech_lead_approval import (
    ApprovalTransition,
    ApprovalVerdictKind,
    OperatorApprovalRecord,
    carries_proposal_marker,
    with_proposal_marker,
)
from issue_orchestrator.domain.tech_lead_session import StoredTechLeadOp
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore
from tests.approval_helpers import (
    ADMITTED,
    BOT,
    CLAIMED,
    CONTRIBUTOR,
    GATED,
    MAINTAINER,
    FakeApprovalEvidence,
    make_approvals,
)


def _issue(number: int, labels, *, state: str = "open") -> Issue:
    return Issue(number=number, title=f"proposal {number}", labels=list(labels), state=state, repo="o/r")


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


# --- verification ---------------------------------------------------------


class TestVerification:
    def test_a_maintainer_label_approves(self) -> None:
        evidence = FakeApprovalEvidence()
        evidence.label(500, by=MAINTAINER)

        verdict = make_approvals(evidence).verify(_issue(500, CLAIMED))

        assert verdict.approved
        assert (verdict.kind, verdict.actor) == (ApprovalVerdictKind.MAINTAINER, MAINTAINER)

    def test_a_bot_label_never_approves_and_no_role_is_read(self) -> None:
        evidence = FakeApprovalEvidence()
        evidence.label(500, by=BOT)

        verdict = make_approvals(evidence).verify(_issue(500, CLAIMED))

        assert verdict.kind is ApprovalVerdictKind.BOT_ACTOR
        assert not verdict.approved and verdict.rejected_claim
        assert evidence.role_reads == []

    def test_a_github_app_event_is_automation_even_with_a_user_login(self) -> None:
        """performed_via_github_app marks automation however the actor is named."""
        from issue_orchestrator.domain.tech_lead_approval import LabelEvent

        evidence = FakeApprovalEvidence()
        evidence.events[(500, "approved", False)] = LabelEvent(77, MAINTAINER, True, "t")

        assert make_approvals(evidence).verify(_issue(500, CLAIMED)).kind is ApprovalVerdictKind.BOT_ACTOR

    @pytest.mark.parametrize("role", ["write", "triage", "read", None])
    def test_a_non_maintainer_role_never_approves(self, role) -> None:
        evidence = FakeApprovalEvidence(roles={CONTRIBUTOR: role} if role else {})
        evidence.label(500, by=CONTRIBUTOR)

        verdict = make_approvals(evidence).verify(_issue(500, CLAIMED))

        assert verdict.kind is ApprovalVerdictKind.NOT_A_MAINTAINER
        assert verdict.rejected_claim

    @pytest.mark.parametrize("role", ["admin", "maintain"])
    def test_maintainer_roles(self, role) -> None:
        evidence = FakeApprovalEvidence(roles={"lead": role})
        evidence.label(500, by="lead")

        assert make_approvals(evidence).verify(_issue(500, CLAIMED)).approved

    def test_an_approved_label_with_no_event_does_not_approve(self) -> None:
        verdict = make_approvals().verify(_issue(500, CLAIMED))

        assert verdict.kind is ApprovalVerdictKind.NO_LABEL_EVENT
        assert verdict.rejected_claim

    def test_events_are_read_only_for_items_carrying_approved(self) -> None:
        """GitHub API discipline: the backlog itself costs no event reads."""
        evidence = FakeApprovalEvidence()
        approvals = make_approvals(evidence)

        verdicts = approvals.verify_claims(
            [_issue(1, GATED), _issue(2, ["tech-lead-proposal"]), _issue(3, ["agent:backend"])]
        )

        assert verdicts == {}
        assert evidence.event_reads == [] and evidence.role_reads == []

    def test_an_approved_label_on_a_closed_proposal_is_not_approval(self) -> None:
        """Escape vector: `approved` on a closed (declined or finished) proposal."""
        evidence = FakeApprovalEvidence()
        evidence.label(500, by=MAINTAINER)

        verdict = make_approvals(evidence).verify(_issue(500, CLAIMED, state="closed"))

        assert verdict.kind is ApprovalVerdictKind.CLOSED
        assert not verdict.approved and not verdict.rejected_claim
        assert evidence.event_reads == []

    def test_a_control_center_approval_counts_only_for_its_own_label_event(self) -> None:
        evidence = FakeApprovalEvidence()
        approvals = make_approvals(evidence)
        event = evidence.engine_write(500)  # the engine's write on the operator's behalf
        approvals.records.record_operator_approval(OperatorApprovalRecord(500, event.event_id, "t"))

        assert approvals.verify(_issue(500, CLAIMED), fresh=True).kind is ApprovalVerdictKind.CONTROL_CENTER
        # Removed and re-applied by automation: a NEW event the record never covered.
        evidence.label(500, by=BOT)
        assert approvals.verify(_issue(500, CLAIMED), fresh=True).kind is ApprovalVerdictKind.BOT_ACTOR


class TestCacheAndRevocation:
    def test_a_verified_approval_is_cached_but_a_fresh_read_bypasses_it(self) -> None:
        evidence = FakeApprovalEvidence()
        evidence.label(500, by=MAINTAINER)
        approvals = make_approvals(evidence)
        approvals.verify(_issue(500, CLAIMED))
        evidence.label(500, by=BOT)

        assert approvals.verify(_issue(500, CLAIMED)).approved  # cached
        assert not approvals.verify(_issue(500, CLAIMED), fresh=True).approved

    def test_approved_added_then_removed_revokes_admission(self) -> None:
        """Escape vector: a maintainer adds `approved`, then it is removed.
        Observing the issue without it drops the verified approval, so a later
        `approved` from anyone else is judged afresh."""
        evidence = FakeApprovalEvidence()
        evidence.label(500, by=MAINTAINER)
        approvals = make_approvals(evidence)
        assert approvals.verify(_issue(500, CLAIMED)).approved
        assert approvals.admits(_issue(500, ADMITTED))

        approvals.observe([_issue(500, ["tech-lead-proposal"])])  # `approved` removed

        assert not approvals.admits(_issue(500, ADMITTED))
        evidence.label(500, by=BOT)  # re-applied by automation
        assert not approvals.verify(_issue(500, ADMITTED)).approved
        assert not approvals.admits(_issue(500, ADMITTED))


class TestAdmission:
    def test_ordinary_work_is_unaffected(self) -> None:
        assert make_approvals().admits(_issue(7, ["agent:backend"]))

    @pytest.mark.parametrize("labels", [GATED, CLAIMED, ("tech-lead-proposal",), ADMITTED])
    def test_labels_alone_never_admit_a_proposal(self, labels) -> None:
        """Escape vector: a strip of `awaiting-approval` plus a bot's (or
        anyone's) `approved` — label state ADMITTED — is not admission without
        a verified approval."""
        assert not make_approvals().admits(_issue(500, labels))

    def test_a_verified_approval_in_the_admitted_state_admits(self) -> None:
        approvals = make_approvals(FakeApprovalEvidence(default_approver=MAINTAINER))
        approvals.verify(_issue(500, ADMITTED))

        assert approvals.admits(_issue(500, ADMITTED))

    def test_the_scheduler_keeps_an_unverified_admitted_proposal_out(self) -> None:
        config = Config()
        approvals = make_approvals()
        scheduler = Scheduler(config, approval_admission=approvals.admits)
        forged = _issue(500, ["agent:backend", *ADMITTED])

        [decision] = scheduler.evaluate_issues([forged], check_dependencies=False)

        assert not decision.available
        assert decision.reason is AvailabilityReason.BLOCKED_LABEL

    def test_the_scheduler_admits_a_verified_approval(self) -> None:
        approvals = make_approvals(FakeApprovalEvidence(default_approver=MAINTAINER))
        issue = _issue(500, ["agent:backend", *ADMITTED])
        approvals.verify(issue)
        scheduler = Scheduler(Config(), approval_admission=approvals.admits)

        [decision] = scheduler.evaluate_issues([issue], check_dependencies=False)

        assert decision.available

    def test_an_unwired_scheduler_admits_no_proposal(self) -> None:
        [decision] = Scheduler(Config()).evaluate_issues(
            [_issue(500, ["agent:backend", *ADMITTED])], check_dependencies=False
        )

        assert not decision.available


class TestRetryAndResetNeverApprove:
    def test_retry_never_clears_approval_labels(self) -> None:
        """Escape vector: an engine retry "clears every blocking label"
        (porchpin#444). It clears the real blocks and leaves approval alone."""
        lm = LabelManager(Config())
        labels = ["agent:backend", "blocked-failed", *GATED]

        removed = labels_to_remove_for_retry(labels, lm, has_open_pr=False)

        assert removed == ["blocked-failed"]

    def test_a_stripped_proposal_stays_blocked_for_retry_callers(self) -> None:
        lm = LabelManager(Config())
        assert labels_to_remove_for_retry(["tech-lead-proposal"], lm, has_open_pr=False) == []
        assert lm.is_blocking_any(["tech-lead-proposal"])


# --- settlement planning ------------------------------------------------------


class TestSettlementPlanning:
    def test_admits_a_verified_follow_up_but_never_an_op_backed_proposal(self) -> None:
        approvals = make_approvals(FakeApprovalEvidence(default_approver=MAINTAINER))
        follow_up, op_backed = _issue(600, CLAIMED), _issue(500, CLAIMED)
        verdicts = approvals.verify_claims([follow_up, op_backed])

        settlements = plan_approval_settlements([follow_up, op_backed], verdicts, op_backed={500})

        assert [(s.issue_number, s.transition) for s in settlements] == [(600, ApprovalTransition.ADMIT)]

    def test_rejects_a_bot_claim(self) -> None:
        evidence = FakeApprovalEvidence()
        evidence.label(600, by=BOT)
        claimed = _issue(600, CLAIMED)
        verdicts = make_approvals(evidence).verify_claims([claimed])

        [settlement] = plan_approval_settlements([claimed], verdicts, op_backed=())

        assert settlement.transition is ApprovalTransition.REJECT_CLAIM

    def test_restores_a_stripped_waiting_label(self) -> None:
        stripped = _issue(600, ["agent:backend", "tech-lead-proposal"])

        [settlement] = plan_approval_settlements([stripped], {}, op_backed=())

        assert settlement.transition is ApprovalTransition.RESTORE_WAITING

    def test_nothing_for_waiting_admitted_or_closed_items(self) -> None:
        approvals = make_approvals(FakeApprovalEvidence(default_approver=MAINTAINER))
        issues = [_issue(1, GATED), _issue(2, ADMITTED), _issue(3, CLAIMED, state="closed")]

        assert plan_approval_settlements(issues, approvals.verify_claims(issues), op_backed=()) == ()


# --- settlement writes -----------------------------------------------------------


def _action(number: int, transition: ApprovalTransition) -> SettleProposalApprovalAction:
    return SettleProposalApprovalAction(issue_number=number, transition=transition)


class TestSettlementWrites:
    def test_admit_takes_the_waiting_label_off_after_a_fresh_check(self) -> None:
        host = MagicMock()
        host.get_issue.return_value = _issue(600, CLAIMED)
        approvals = make_approvals(FakeApprovalEvidence(default_approver=MAINTAINER))

        result = apply_settle_proposal_approval(
            _action(600, ApprovalTransition.ADMIT), approvals=approvals, repository=host
        )

        assert result.success
        host.remove_label.assert_called_once_with(600, "awaiting-approval")
        assert approvals.admits(_issue(600, ADMITTED))

    def test_admit_refuses_when_the_fresh_check_no_longer_approves(self) -> None:
        evidence = FakeApprovalEvidence()
        evidence.label(600, by=MAINTAINER)
        approvals = make_approvals(evidence)
        approvals.verify(_issue(600, CLAIMED))  # planned on a maintainer's label
        evidence.label(600, by=BOT)  # then automation re-applied it
        host = MagicMock()
        host.get_issue.return_value = _issue(600, CLAIMED)

        result = apply_settle_proposal_approval(
            _action(600, ApprovalTransition.ADMIT), approvals=approvals, repository=host
        )

        assert not result.success
        host.remove_label.assert_not_called()

    def test_admitting_a_proposal_closed_in_the_meantime_writes_nothing(self) -> None:
        """Escape vector: `approved` racing a decline. The decline closed the
        issue before the admission applied, so nothing is admitted."""
        host = MagicMock()
        host.get_issue.return_value = _issue(600, CLAIMED, state="closed")
        approvals = make_approvals(FakeApprovalEvidence(default_approver=MAINTAINER))

        result = apply_settle_proposal_approval(
            _action(600, ApprovalTransition.ADMIT), approvals=approvals, repository=host
        )

        assert result.success and result.details["settled"] == "closed"
        host.remove_label.assert_not_called()
        host.add_comment.assert_not_called()

    def test_reject_removes_the_approved_label_and_restores_waiting(self) -> None:
        evidence = FakeApprovalEvidence()
        evidence.label(600, by=BOT)
        host = MagicMock()
        host.get_issue.return_value = _issue(600, ADMITTED)

        result = apply_settle_proposal_approval(
            _action(600, ApprovalTransition.REJECT_CLAIM), approvals=make_approvals(evidence), repository=host
        )

        assert result.success
        host.add_label.assert_called_once_with(600, "awaiting-approval")
        host.remove_label.assert_called_once_with(600, "approved")
        [(number, body), _] = host.add_comment.call_args
        assert number == 600 and BOT in body

    def test_restore_waiting(self) -> None:
        host = MagicMock()
        host.get_issue.return_value = _issue(600, ["tech-lead-proposal"])

        result = apply_settle_proposal_approval(
            _action(600, ApprovalTransition.RESTORE_WAITING), approvals=make_approvals(), repository=host
        )

        assert result.success
        host.add_label.assert_called_once_with(600, "awaiting-approval")

    def test_unwired_owner_fails_loudly(self) -> None:
        result = apply_settle_proposal_approval(
            _action(600, ApprovalTransition.ADMIT), approvals=None, repository=MagicMock()
        )

        assert not result.success


# --- the Control Center command -------------------------------------------------


class _Host:
    """Just enough GitHub: labels write label events through the evidence fake."""

    def __init__(self, evidence: FakeApprovalEvidence, issue: Issue) -> None:
        self.evidence = evidence
        self.issue = issue
        self.comments: list[tuple[int, str]] = []
        self.states: list[tuple[int, str]] = []

    def get_issue(self, number: int) -> Issue:
        return self.issue

    def add_label(self, number: int, label: str) -> None:
        self.issue = _issue(number, [*self.issue.labels, label], state=self.issue.state)
        self.evidence.engine_write(number, label)

    def remove_label(self, number: int, label: str) -> None:
        self.issue = _issue(number, [l for l in self.issue.labels if l != label], state=self.issue.state)

    def add_comment(self, number: int, body: str) -> str:
        self.comments.append((number, body))
        return "url"

    def update_issue_state(self, number: int, state: str) -> None:
        self.states.append((number, state))
        self.issue = _issue(number, self.issue.labels, state=state)


class TestOperatorCommand:
    def test_approve_records_the_engine_write_as_the_operators_approval(self) -> None:
        evidence = FakeApprovalEvidence()
        approvals = make_approvals(evidence)
        host = _Host(evidence, _issue(500, GATED))
        ops = InMemoryTechLeadAuthorityStore()
        ops.record_op(issue_number=500, op=_op())

        outcome = apply_operator_proposal_command(
            TechLeadProposalCommand(500, "approve"), repository=host, ops=ops, approvals=approvals
        )

        assert outcome.outcome == "approved"
        assert "approved" in host.issue.labels
        verdict = approvals.verify(host.issue, fresh=True)
        assert verdict.kind is ApprovalVerdictKind.CONTROL_CENTER
        assert ops.load_op(issue_number=500) is not None  # recorded, never executed here

    def test_approve_replaces_a_bot_label_so_it_binds_a_fresh_event(self) -> None:
        evidence = FakeApprovalEvidence()
        evidence.label(500, by=BOT)
        approvals = make_approvals(evidence)
        host = _Host(evidence, _issue(500, CLAIMED))

        outcome = apply_operator_proposal_command(
            TechLeadProposalCommand(500, "approve"), repository=host,
            ops=InMemoryTechLeadAuthorityStore(), approvals=approvals,
        )

        assert outcome.outcome == "approved"
        assert approvals.verify(host.issue, fresh=True).approved

    def test_decline_closes_and_discards_the_op(self) -> None:
        evidence = FakeApprovalEvidence()
        approvals = make_approvals(evidence)
        host = _Host(evidence, _issue(500, GATED))
        ops = InMemoryTechLeadAuthorityStore()
        ops.record_op(issue_number=500, op=_op())

        outcome = apply_operator_proposal_command(
            TechLeadProposalCommand(500, "decline"), repository=host, ops=ops, approvals=approvals
        )

        assert outcome.outcome == "declined"
        assert host.states == [(500, "closed")]
        assert ops.load_op(issue_number=500) is None

    def test_approving_after_a_decline_is_unavailable(self) -> None:
        """Escape vector: `approved` racing a decline. Once declined (closed),
        an approval is refused and nothing is labelled."""
        evidence = FakeApprovalEvidence()
        host = _Host(evidence, _issue(500, GATED, state="closed"))

        outcome = apply_operator_proposal_command(
            TechLeadProposalCommand(500, "approve"), repository=host,
            ops=InMemoryTechLeadAuthorityStore(), approvals=make_approvals(evidence),
        )

        assert outcome.outcome == "unavailable"
        assert "approved" not in host.issue.labels

    def test_a_non_proposal_cannot_be_approved(self) -> None:
        evidence = FakeApprovalEvidence()
        host = _Host(evidence, _issue(7, ["agent:backend"]))

        outcome = apply_operator_proposal_command(
            TechLeadProposalCommand(7, "approve"), repository=host,
            ops=InMemoryTechLeadAuthorityStore(), approvals=make_approvals(evidence),
        )

        assert outcome.outcome == "unavailable"
        assert host.issue.labels == ["agent:backend"]


# --- review round 1 (#7763) -------------------------------------------------


class TestBodyMarkerSurvivesAFullStrip:
    """F1: stripping EVERY approval label must not turn a proposal into work."""

    def _stripped(self, number: int = 700) -> Issue:
        return Issue(
            number=number, title="t", labels=["agent:backend"], repo="o/r",
            body=with_proposal_marker("follow-up body"),
        )

    def test_the_scheduler_refuses_a_fully_stripped_proposal(self) -> None:
        scheduler = Scheduler(Config(), approval_admission=make_approvals().admits)

        [decision] = scheduler.evaluate_issues([self._stripped()], check_dependencies=False)

        assert not decision.available

    def test_reconciliation_restores_both_gate_labels(self) -> None:
        [settlement] = plan_approval_settlements([self._stripped()], {}, op_backed=())
        host = MagicMock()
        host.get_issue.return_value = self._stripped()

        result = apply_settle_proposal_approval(
            _action(700, settlement.transition), approvals=make_approvals(), repository=host
        )

        assert settlement.transition is ApprovalTransition.RESTORE_WAITING and result.success
        assert [c.args for c in host.add_label.call_args_list] == [
            (700, "tech-lead-proposal"), (700, "awaiting-approval"),
        ]

    def test_gated_promotions_carry_the_marker(self) -> None:
        from issue_orchestrator.control.tech_lead_finding_promotion import build_promotion_issue_body
        from issue_orchestrator.domain.tech_lead_findings import PatternEvidence, PromotableFinding

        evidence = PatternEvidence(
            signature="sig", case_file_issue_number=65, observation_count=3,
            fix_class="code", area="a", diagnosis="d",
        )
        finding = PromotableFinding(evidence=evidence, target_repo="o/r")

        assert carries_proposal_marker(build_promotion_issue_body(finding, source_repo="s/r", gated=True))
        assert not carries_proposal_marker(build_promotion_issue_body(finding, source_repo="s/r", gated=False))


class TestTheLaunchBoundaryRechecksFresh:
    """F2: the scheduler's cache can be a tick stale; launch reads fresh."""

    @staticmethod
    def _launch(number: int):
        from issue_orchestrator.control.actions import LaunchSessionAction

        return LaunchSessionAction(number=number)

    def test_a_bot_reapplied_approval_between_ticks_is_not_launched(self) -> None:
        from issue_orchestrator.control.tech_lead_approval import refuse_unapproved_proposal_launch

        evidence = FakeApprovalEvidence()
        evidence.label(700, by=MAINTAINER)
        approvals = make_approvals(evidence)
        admitted = _issue(700, ["agent:backend", *ADMITTED])
        approvals.verify(admitted)
        assert approvals.admits(admitted)  # the scheduler's view: still verified
        evidence.label(700, by=BOT)  # removed and re-applied by a bot, unobserved
        host = MagicMock()
        host.get_issue.return_value = admitted

        refused = refuse_unapproved_proposal_launch(self._launch(700), host, approvals)

        assert refused is not None and not refused.success

    def test_a_demoted_approver_is_read_fresh_at_launch(self) -> None:
        from issue_orchestrator.control.tech_lead_approval import refuse_unapproved_proposal_launch

        evidence = FakeApprovalEvidence()
        evidence.label(700, by=MAINTAINER)
        approvals = make_approvals(evidence)
        admitted = _issue(700, ["agent:backend", *ADMITTED])
        approvals.verify(admitted)
        evidence.roles[MAINTAINER] = "write"  # demoted after approving
        host = MagicMock()
        host.get_issue.return_value = admitted
        reads_before = len(evidence.role_reads)

        refused = refuse_unapproved_proposal_launch(self._launch(700), host, approvals)

        assert refused is not None
        assert len(evidence.role_reads) == reads_before + 1  # a fresh role read

    def test_a_standing_approval_and_ordinary_work_launch(self) -> None:
        from issue_orchestrator.control.tech_lead_approval import refuse_unapproved_proposal_launch

        approvals = make_approvals(FakeApprovalEvidence(default_approver=MAINTAINER))
        host = MagicMock()
        host.get_issue.return_value = _issue(700, ["agent:backend", *ADMITTED])
        assert refuse_unapproved_proposal_launch(self._launch(700), host, approvals) is None
        host.get_issue.return_value = _issue(701, ["agent:backend"])
        assert refuse_unapproved_proposal_launch(self._launch(701), host, approvals) is None

    def test_the_applier_refuses_before_any_launch(self) -> None:
        from tests.runtime_lifecycle_helpers import make_action_applier

        host = MagicMock()
        host.get_issue.return_value = _issue(700, ["agent:backend", *ADMITTED])
        launcher = MagicMock()
        applier = make_action_applier(
            labels=MagicMock(), sessions=MagicMock(), events=MagicMock(),
            repository_host=host, session_launcher=launcher,
        )
        applier.tech_lead_approvals = make_approvals()

        result = applier.apply(self._launch(700))

        assert not result.success
        launcher.assert_not_called()


class TestControlCenterApprovalIsAttributed:
    """F3: only the engine's OWN write is ever bound to an operator approval."""

    def test_a_relabel_racing_the_engine_write_is_not_bound(self) -> None:
        evidence = FakeApprovalEvidence()
        approvals = make_approvals(evidence)

        class _RacedHost(_Host):
            def add_label(self, number: int, label: str) -> None:
                super().add_label(number, label)
                self.evidence.label(number, label, by=BOT)  # removed and re-added by a bot

        host = _RacedHost(evidence, _issue(500, GATED))

        outcome = apply_operator_proposal_command(
            TechLeadProposalCommand(500, "approve"), repository=host,
            ops=InMemoryTechLeadAuthorityStore(), approvals=approvals,
        )

        assert outcome.outcome == "failed"
        assert approvals.records.load_operator_approval(500) is None
        assert not approvals.verify(host.issue, fresh=True).approved

    def test_a_record_never_vouches_for_an_event_the_engine_did_not_write(self) -> None:
        evidence = FakeApprovalEvidence()
        approvals = make_approvals(evidence)
        event = evidence.label(500, by=BOT)
        approvals.records.record_operator_approval(OperatorApprovalRecord(500, event.event_id, "t"))

        assert approvals.verify(_issue(500, CLAIMED), fresh=True).kind is ApprovalVerdictKind.BOT_ACTOR


def test_a_personal_token_engine_approves_as_its_maintainer_user() -> None:
    """No App identity: the engine's write is its user's, judged by the actor
    check alone — no record is bound, and a maintainer user's label approves."""
    evidence = FakeApprovalEvidence()
    approvals = make_approvals(evidence)

    class _TokenHost(_Host):
        def add_label(self, number: int, label: str) -> None:
            self.issue = _issue(number, [*self.issue.labels, label], state=self.issue.state)
            self.evidence.label(number, label, by=MAINTAINER)

    host = _TokenHost(evidence, _issue(500, GATED))

    outcome = apply_operator_proposal_command(
        TechLeadProposalCommand(500, "approve"), repository=host,
        ops=InMemoryTechLeadAuthorityStore(), approvals=approvals,
    )

    assert outcome.outcome == "approved"
    assert approvals.records.load_operator_approval(500) is None
    assert approvals.verify(host.issue, fresh=True).kind is ApprovalVerdictKind.MAINTAINER
