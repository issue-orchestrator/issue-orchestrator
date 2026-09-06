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
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.tech_lead_session import StoredTechLeadOp
from issue_orchestrator.ports.comment_receipt import IssueCommentReceipt
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore
from issue_orchestrator.infra.config import Config


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
        read_issue=host.get_issue, has_active_issue_runtime=lambda n: False,
        run_reset=lambda n, labels: ResetRetryRunOutcome(success=True))
    kill = TechLeadKillSessionExecutor(events=MagicMock(), run_kill=lambda target, reason: KillSessionRunOutcome(success=True),
        read_generation_stale_reason=lambda target: None)
    applier = ActionApplier(labels=MagicMock(), sessions=MagicMock(), events=MagicMock(),
        repository_host=host, tech_lead_ops=authority, tech_lead_reset_retry=reset, tech_lead_kill_session=kill)
    action = ReuseTechLeadProposalAction(number=7000, comment="Reuse current remedy", required_op=op)
    return host, authority, reset, kill, applier, action


@pytest.mark.parametrize("condition", ["closed", "missing", "no-op", "unblocked", "active", "write-race"])
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
        reset.has_active_issue_runtime = lambda n: True
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
