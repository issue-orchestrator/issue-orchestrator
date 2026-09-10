"""Required diagnosis and proposal reuse at planner/executor boundaries."""
from dataclasses import replace
import hashlib
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import AddCommentAction
from issue_orchestrator.control.required_issue_comment import ReuseTechLeadProposalAction
from issue_orchestrator.control.tech_lead_completion_gate import evaluate_required_act_level_outcome
from issue_orchestrator.control.tech_lead_reset_retry import TechLeadResetRetryExecutor, ResetRetryRunOutcome, apply_completion_actions_gated
from issue_orchestrator.control.tech_lead_kill_session import TechLeadKillSessionExecutor, KillSessionRunOutcome
from issue_orchestrator.control.tech_lead_validated_work_recovery import (
    TechLeadValidatedWorkRecoveryExecutor,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.tech_lead_session import StoredTechLeadOp
from issue_orchestrator.domain.recovery_attempt import (
    RecoveryAttemptPending,
    RecoveryAuthorityStale,
)
from issue_orchestrator.domain.validated_work import (
    RemoteBaselineStatus,
    ValidatedWorkFailure,
    ValidatedWorkKey,
)
from issue_orchestrator.domain.validated_work_commands import (
    ValidatedWorkAuthoritySnapshot,
)
from issue_orchestrator.ports.comment_receipt import IssueCommentReceipt
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore
from issue_orchestrator.infra.config import Config
from tests.runtime_lifecycle_helpers import reset_snapshot
from issue_orchestrator.control.review_exchange_lifecycle import IssueRuntimeActivity, IssueRuntimeOwnerKind


class Host:
    def __init__(self):
        self.states = {7000: "open", 6410: "open"}
        self.issue = Issue(number=6410, title="blocked", labels=["blocked-failed"])
        self.comments = []
        self.after_comment = lambda: None

    def get_issue_state(self, number):
        return self.states[number]

    def get_issue(self, number):
        assert number == 6410
        return self.issue

    def find_issue_comment_receipt(self, number, *, body):
        if (number, body) not in self.comments:
            return None
        return IssueCommentReceipt("1", "https://example.test/comment", "user:7", hashlib.sha256(body.encode()).hexdigest())

    def add_comment(self, number, body):
        self.comments.append((number, body))
        self.after_comment()
        return "https://example.test/comment"


def harness(op_type="reset_retry"):
    host, authority = Host(), InMemoryTechLeadAuthorityStore()
    op = StoredTechLeadOp(op_type=op_type, target_issue_number=6410,
        rationale="remedy", source_run_id="source", source_session_name="session",
        source_action_id="A1", created_at="2026-08-09T00:00:00+00:00",
        target_session_id="run-1" if op_type == "kill_hung_session" else "",
        target_terminal_id="term-1" if op_type == "kill_hung_session" else "",
        target_session_type="code" if op_type == "kill_hung_session" else "")
    authority.record_op(issue_number=7000, op=op)
    reset = TechLeadResetRetryExecutor(events=MagicMock(), label_manager=LabelManager(Config()),
        read_issue=host.get_issue, runtime_snapshot=reset_snapshot,
        run_reset=lambda n, labels: ResetRetryRunOutcome(success=True))
    kill = TechLeadKillSessionExecutor(events=MagicMock(), run_kill=lambda target, reason: KillSessionRunOutcome(success=True),
        read_generation_stale_reason=lambda target: None)
    applier = ActionApplier(labels=MagicMock(), sessions=MagicMock(), events=MagicMock(),
        repository_host=host, tech_lead_ops=authority, tech_lead_reset_retry=reset, tech_lead_kill_session=kill)
    action = ReuseTechLeadProposalAction(number=7000, comment="Reuse current remedy", required_op=op)
    return host, authority, reset, kill, applier, action


def _recovery_authority(revision: int = 1) -> ValidatedWorkAuthoritySnapshot:
    key = ValidatedWorkKey("owner/repo", 6410, "issue-6410", "a" * 40)
    return ValidatedWorkAuthoritySnapshot(
        record_id=key.record_id,
        evidence_id="evidence-6410",
        observation_revision=revision,
        validated_head_sha=key.validated_head_sha,
        branch_name=key.branch_name,
        repo_slug=key.repo_slug,
        issue_number=key.issue_number,
        pr_number=71,
        expected_remote_head_sha="b" * 40,
        remote_baseline_status=RemoteBaselineStatus.OBSERVED,
    )


def recovery_harness(preflight):
    host, authority = Host(), InMemoryTechLeadAuthorityStore()
    snapshot = _recovery_authority()
    op = StoredTechLeadOp(
        op_type="recover_validated_work",
        target_issue_number=6410,
        rationale="recover retained work",
        source_run_id="source",
        source_session_name="session",
        source_action_id="A3",
        created_at="2026-09-08T00:00:00+00:00",
        validated_work_authority=snapshot,
    )
    authority.record_op(issue_number=7000, op=op)
    recovery = TechLeadValidatedWorkRecoveryExecutor(
        events=MagicMock(), preflight=preflight, recover=MagicMock()
    )
    applier = ActionApplier(
        labels=MagicMock(),
        sessions=MagicMock(),
        events=MagicMock(),
        repository_host=host,
        tech_lead_ops=authority,
        recover_validated_work=recovery,
    )
    action = ReuseTechLeadProposalAction(
        number=7000, comment="Reuse current recovery", required_op=op
    )
    return host, applier, action, recovery


@pytest.mark.parametrize("condition", ["closed", "missing", "no-op", "unblocked", "active", "validated-work", "write-race"])
def test_inapplicable_reuse_withholds_completion(condition):
    host, authority, reset, kill, applier, action = harness()
    if condition == "closed":
        host.states[7000] = "closed"
    elif condition == "missing":
        host.states[7000] = None
    elif condition == "no-op":
        authority.discard_op(issue_number=7000)
    elif condition == "unblocked":
        host.issue = replace(host.issue, labels=[])
    elif condition == "active":
        reset.runtime_snapshot = lambda n: reset_snapshot(n, busy=True)
    elif condition == "validated-work":
        reset.runtime_snapshot = lambda n: replace(reset_snapshot(n), activity=IssueRuntimeActivity(
            frozenset({IssueRuntimeOwnerKind.VALIDATED_WORK}), frozenset()))
    else:
        host.after_comment = lambda: host.states.update({7000: "closed"})
    results, error = apply_completion_actions_gated(applier, [
        AddCommentAction(number=6410, comment="success-only"), action], issue_number=6410)
    assert error is None
    assert evaluate_required_act_level_outcome(results).failed
    assert (6410, "success-only") not in host.comments


@pytest.mark.parametrize("condition", ["recorded-replacement", "live-replacement", "no-live-reader"])
def test_kill_reuse_must_still_own_exact_live_generation(condition):
    host, authority, reset, kill, applier, action = harness("kill_hung_session")
    if condition == "recorded-replacement":
        action = replace(action, required_op=replace(action.required_op, target_session_id="new-run"))
    elif condition == "live-replacement":
        kill.read_generation_stale_reason = lambda target: "live generation replaced"
    else:
        kill.read_generation_stale_reason = None
    result = applier.apply(action)
    assert not result.success
    assert evaluate_required_act_level_outcome([result]).failed
    assert host.comments == []


@pytest.mark.parametrize("op_type", ["reset_retry", "kill_hung_session"])
def test_valid_proposal_reuse_proves_receipt_without_executing_or_reapproving(op_type):
    host, authority, reset, kill, applier, action = harness(op_type)
    reset.run_reset = lambda *args: pytest.fail("reuse executed a reset")
    kill.run_kill = lambda *args: pytest.fail("reuse killed a worker")
    result = applier.apply(action)
    assert result.success
    assert result.details["author_key"] == "user:7"
    assert applier.apply(action).success
    assert host.comments == [(7000, action.comment)]
    assert authority.load_op(issue_number=7000) == action.required_op


def test_current_recovery_proposal_is_reusable_without_executing_it() -> None:
    host, applier, action, recovery = recovery_harness(lambda _command: None)

    result = applier.apply(action)

    assert result.success
    assert host.comments == [(7000, action.comment)]
    recovery.recover.assert_not_called()


def test_stale_recovery_proposal_reuse_withholds_success_only_effects() -> None:
    approved = _recovery_authority()
    current = replace(approved, observation_revision=2)
    stale = RecoveryAuthorityStale(approved, current)
    host, applier, action, recovery = recovery_harness(
        lambda _command: RecoveryAttemptPending(
            stale.describe(),
            ValidatedWorkFailure.AUTHORITY_SNAPSHOT_STALE,
            stale,
        )
    )

    results, error = apply_completion_actions_gated(
        applier,
        [AddCommentAction(number=6410, comment="success-only"), action],
        issue_number=6410,
    )

    assert error is None
    assert evaluate_required_act_level_outcome(results).failed
    assert host.comments == []
    recovery.recover.assert_not_called()


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("publication", ["receipt", "unverified", "failed"])
def test_comment_owner_uses_injected_publisher_and_checks_required_receipt(required, publication):
    from issue_orchestrator.control.required_issue_comment import RequiredIssueCommentAction, apply_issue_comment
    host, calls = Host(), []
    host.add_comment = lambda *args: pytest.fail("policy owner reached direct write port")
    original_receipt = host.find_issue_comment_receipt
    def receipt(number, *, body):
        calls.append("receipt")
        return original_receipt(number, body=body)
    host.find_issue_comment_receipt = receipt
    def publish(number, body):
        calls.append("publish")
        if publication == "failed":
            raise RuntimeError("publisher failed")
        if publication == "receipt":
            host.comments.append((number, body))
        return "https://example.test/comment"
    action_type = RequiredIssueCommentAction if required else AddCommentAction
    result = apply_issue_comment(action_type(number=6410, comment="diagnosis"),
        host=host, post_comment=publish, events=MagicMock(), authority=None, reset=None, kill=None,
        require_expected=lambda action, number: calls.append("expected"),
        verify_claim=lambda action, number: calls.append("claim"))
    assert result.success is (publication != "failed" and (not required or publication == "receipt"))
    if required:
        assert calls[:5] == ["expected", "claim", "receipt", "expected", "claim"]
        if publication != "failed":
            assert calls[5:] == ["publish", "receipt", "expected", "claim"]
    else:
        assert calls == ["expected", "claim", "publish"]
