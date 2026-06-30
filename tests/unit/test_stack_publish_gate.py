"""Tests for the stack publish-gate owner (ADR-0029, #6596).

The owner bridges the completion processor (which knows an issue number + a
worktree) and the single dependency-gate evaluator: it reads the issue, asks for
the publish decision, and returns the predecessor base branch or a blocked
diagnostic. These tests wire a *real* DependencyEvaluator so the whole publish
vertical (reader -> evaluator -> gate report -> base/verdict) is exercised.
"""

from pathlib import Path

from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator
from issue_orchestrator.control.stack_publish_gate import StackPublishGate
from issue_orchestrator.domain.dependencies import DependencyTarget
from issue_orchestrator.domain.dependency_gates import PredecessorFacts
from issue_orchestrator.ports import NullEventSink


class _Issue:
    def __init__(self, body, milestone="M7"):
        self.body = body
        self.milestone = milestone


class _IssueReader:
    def __init__(self, issue):
        self._issue = issue
        self.raise_exc: Exception | None = None

    def get_issue(self, issue_number):
        if self.raise_exc is not None:
            raise self.raise_exc
        return self._issue


class _Checker:
    """Predecessor issue state/milestone for the evaluator."""

    def __init__(self, state="open", milestone="M7"):
        self._state = state
        self._milestone = milestone

    def get_issue_state(self, issue_number, repo=None):
        return self._state

    def get_issue_milestone(self, issue_number, repo=None):
        return self._milestone


class _FactsProvider:
    def __init__(self, facts):
        self._facts = facts

    def gather_facts(self, targets):
        return {t: self._facts[t] for t in targets if t in self._facts}


class _Ancestry:
    def __init__(self, stale=None):
        self._stale = stale or set()

    def successor_contains_predecessor(self, worktree, predecessor_branch):
        return predecessor_branch not in self._stale


def _evaluator(checker, facts, ancestry=None):
    return DependencyEvaluator(
        issue_checker=checker,
        events=NullEventSink(),
        predecessor_facts_provider=_FactsProvider(facts),
        branch_ancestry=ancestry,
    )


def _ready_facts(branch="20-base"):
    return {DependencyTarget(20): PredecessorFacts(
        branch_usable=True, validation_passed=True, agent_reviewed=True,
        branch_name=branch, head_sha="abc123",
    )}


def test_non_stack_issue_is_inert():
    gate = StackPublishGate(
        evaluator=_evaluator(_Checker(), {}),
        issue_reader=_IssueReader(_Issue("Depends-on: #5")),
    )
    decision = gate.decide(2, Path("/wt"))
    assert decision.is_stack is False
    assert decision.allowed is True
    assert decision.base_branch is None


def test_stack_successor_bases_on_predecessor_branch():
    gate = StackPublishGate(
        evaluator=_evaluator(_Checker(state="open"), _ready_facts()),
        issue_reader=_IssueReader(_Issue("Stack-after: #20")),
    )
    decision = gate.decide(2, Path("/wt"))
    assert decision.is_stack is True
    assert decision.allowed is True
    assert decision.base_branch == "20-base"


def test_incompatible_base_branch_blocks_with_reason():
    gate = StackPublishGate(
        evaluator=_evaluator(_Checker(state="open"), _ready_facts("20-base")),
        issue_reader=_IssueReader(_Issue("Stack-after: #20")),
        configured_base_branch="release/9",  # conflicts with 20-base
    )
    decision = gate.decide(2, Path("/wt"))
    assert decision.is_stack is True
    assert decision.allowed is False
    assert decision.base_branch is None
    assert "base_branch_conflict" in (decision.reason or "")


def test_ambiguous_stack_base_blocks_publish():
    # F4: two ready unmerged predecessors with distinct usable branches give no
    # single base; the gate must block publish rather than allow a fallback to
    # the processor's default base.
    facts = {
        DependencyTarget(20): PredecessorFacts(
            branch_usable=True, validation_passed=True, agent_reviewed=True,
            branch_name="20-base", head_sha="aaa",
        ),
        DependencyTarget(21): PredecessorFacts(
            branch_usable=True, validation_passed=True, agent_reviewed=True,
            branch_name="21-base", head_sha="bbb",
        ),
    }
    gate = StackPublishGate(
        evaluator=_evaluator(_Checker(state="open"), facts),
        issue_reader=_IssueReader(_Issue("Stack-after: #20\nStack-after: #21")),
    )
    decision = gate.decide(2, Path("/wt"))
    assert decision.is_stack is True
    assert decision.allowed is False
    assert decision.base_branch is None
    assert "ambiguous_stack_base" in (decision.reason or "")


def test_stale_successor_blocks_publish():
    gate = StackPublishGate(
        evaluator=_evaluator(
            _Checker(state="open"), _ready_facts(), ancestry=_Ancestry(stale={"20-base"})
        ),
        issue_reader=_IssueReader(_Issue("Stack-after: #20")),
    )
    decision = gate.decide(2, Path("/wt"))
    assert decision.allowed is False
    assert "predecessor_branch_advanced" in (decision.reason or "")


def test_issue_read_failure_blocks_publish_retryably():
    reader = _IssueReader(_Issue("Stack-after: #20"))
    reader.raise_exc = RuntimeError("transient")
    gate = StackPublishGate(
        evaluator=_evaluator(_Checker(state="open"), _ready_facts()),
        issue_reader=reader,
    )
    decision = gate.decide(2, Path("/wt"))
    # Fail-closed: the gate cannot prove this is not a stack successor, so a read
    # error must block publish (retryably) rather than let a wrong-base PR open.
    assert decision.allowed is False
    assert decision.retryable is True
    assert "could not read issue #2" in (decision.reason or "")


def test_missing_issue_blocks_publish_retryably():
    gate = StackPublishGate(
        evaluator=_evaluator(_Checker(), {}),
        issue_reader=_IssueReader(None),
    )
    decision = gate.decide(2, Path("/wt"))
    # A managed publish whose issue cannot be found also fails closed.
    assert decision.allowed is False
    assert decision.retryable is True
    assert "no issue #2" in (decision.reason or "")
