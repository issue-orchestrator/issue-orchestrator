"""Tests for typed dependency edges and the dependency gate report (ADR-0029).

These cover:
- Parsing normal (``Depends-on:``) and stack (``Stack-after:``) edges.
- Structural problems: self-dependencies, cycles, duplicate declarations,
  mode conflicts, malformed references, missing predecessors.
- Mixed normal/stack declarations on one issue.
- Cross-milestone validation: same milestone, foundation milestone, invalid
  cross-milestone, and the bounded valid same-stack exception.
- Gate reports for normal dependencies and stack predecessors, including
  machine-readable reason codes for blocked gates.
- That a downstream caller can decide work/readiness from the gate report
  without inspecting raw dependency internals.
"""

import pytest

from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator
from issue_orchestrator.domain.dependencies import (
    Dependency,
    DependencyMode,
    DependencyState,
    EdgeProblem,
    parse_dependency_edges,
)
from issue_orchestrator.domain.dependency_gates import (
    Gate,
    GateBlockReason,
    PredecessorFacts,
    build_gate_report,
    detect_cycles,
)
from issue_orchestrator.ports import NullEventSink


class MockIssueChecker:
    """Mock issue checker with per-issue state and milestone."""

    def __init__(self):
        self.state: dict[int, str] = {}
        self.milestone: dict[int, str | None] = {}
        self.error_on: set[int] = set()

    def add(self, number: int, state: str, milestone: str | None = "M1"):
        self.state[number] = state
        self.milestone[number] = milestone

    def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
        if issue_number in self.error_on:
            raise RuntimeError("API error")
        return self.state.get(issue_number)

    def get_issue_milestone(self, issue_number: int, repo: str | None = None) -> str | None:
        if issue_number in self.error_on:
            raise RuntimeError("API error")
        return self.milestone.get(issue_number)


@pytest.fixture
def checker():
    return MockIssueChecker()


@pytest.fixture
def evaluator(checker):
    return DependencyEvaluator(issue_checker=checker, events=NullEventSink())


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


class TestParseDependencyEdges:
    def test_parse_normal_edge(self):
        edges = parse_dependency_edges("Depends-on: #10")
        assert len(edges) == 1
        assert edges[0].mode is DependencyMode.NORMAL
        assert edges[0].issue_number == 10
        assert edges[0].problem is None

    def test_parse_stack_edge(self):
        edges = parse_dependency_edges("Stack-after: #20")
        assert len(edges) == 1
        assert edges[0].mode is DependencyMode.STACK
        assert edges[0].issue_number == 20

    def test_parse_mixed_normal_and_stack(self):
        body = "Depends-on: #10\nStack-after: #20"
        edges = parse_dependency_edges(body)
        modes = {(e.issue_number): e.mode for e in edges}
        assert modes == {10: DependencyMode.NORMAL, 20: DependencyMode.STACK}

    def test_parse_external_id_and_cross_repo(self):
        body = "Depends-on: M1-010\nStack-after: acme/widgets#5"
        edges = parse_dependency_edges(body)
        assert edges[0].external_id == "M1-010"
        assert edges[0].mode is DependencyMode.NORMAL
        assert edges[1].issue_number == 5
        assert edges[1].repository == "acme/widgets"
        assert edges[1].mode is DependencyMode.STACK

    def test_parse_records_source_location(self):
        body = "intro line\nStack-after: #7\n"
        edges = parse_dependency_edges(body)
        assert edges[0].source_line == 2
        assert edges[0].source_text == "#7"

    def test_malformed_stack_reference_is_flagged_not_dropped(self):
        edges = parse_dependency_edges("Stack-after: not-a-ref")
        assert len(edges) == 1
        assert edges[0].problem is EdgeProblem.MALFORMED_REFERENCE
        assert edges[0].issue_number is None
        assert edges[0].source_text == "not-a-ref"

    def test_case_insensitive_keywords(self):
        edges = parse_dependency_edges("STACK-AFTER: #1\ndepends-on: #2")
        assert edges[0].mode is DependencyMode.STACK
        assert edges[1].mode is DependencyMode.NORMAL

    def test_no_directives_returns_empty(self):
        assert parse_dependency_edges("just a body") == []


# --------------------------------------------------------------------------- #
# Cycle detection (pure)
# --------------------------------------------------------------------------- #


class TestDetectCycles:
    def test_simple_cycle(self):
        assert detect_cycles({1: [2], 2: [3], 3: [1]}) == frozenset({1, 2, 3})

    def test_self_loop(self):
        assert detect_cycles({5: [5]}) == frozenset({5})

    def test_acyclic(self):
        assert detect_cycles({1: [2], 2: [3], 3: []}) == frozenset()

    def test_node_outside_cycle_excluded(self):
        # 4 -> 3 feeds into the 1-2-3 cycle but is not itself cyclic.
        assert detect_cycles({1: [2], 2: [3], 3: [1], 4: [3]}) == frozenset({1, 2, 3})


# --------------------------------------------------------------------------- #
# Pure gate-report builder
# --------------------------------------------------------------------------- #


class TestBuildGateReport:
    def test_no_dependencies_all_gates_open(self):
        report = build_gate_report(1, [])
        assert report.all_open
        assert report.blocked_gates() == ()

    def test_normal_open_blocks_all_gates(self):
        dep = Dependency(issue_number=10, mode=DependencyMode.NORMAL, state=DependencyState.UNSATISFIED)
        report = build_gate_report(1, [dep])
        assert report.blocked_gates() == (Gate.WORK, Gate.REVIEW, Gate.PUBLISH, Gate.MERGE)
        for gate in Gate:
            assert report.reason_codes(gate) == (GateBlockReason.DEPENDENCY_OPEN,)

    def test_normal_satisfied_opens_all_gates(self):
        dep = Dependency(issue_number=10, mode=DependencyMode.NORMAL, state=DependencyState.SATISFIED)
        report = build_gate_report(1, [dep])
        assert report.all_open

    def test_stack_unblocks_work_review_publish_keeps_merge_ordered(self):
        dep = Dependency(issue_number=20, mode=DependencyMode.STACK, state=DependencyState.UNSATISFIED)
        facts = {20: PredecessorFacts(branch_usable=True, validation_passed=True, agent_reviewed=True)}
        report = build_gate_report(1, [dep], facts)
        assert report.can_start_work
        assert report.can_review
        assert report.can_publish
        assert not report.can_merge
        assert report.reason_codes(Gate.MERGE) == (GateBlockReason.PREDECESSOR_NOT_MERGED,)

    def test_stack_without_facts_blocks_work_with_reason_codes(self):
        dep = Dependency(issue_number=20, mode=DependencyMode.STACK, state=DependencyState.UNSATISFIED)
        report = build_gate_report(1, [dep])
        assert set(report.reason_codes(Gate.WORK)) == {
            GateBlockReason.PREDECESSOR_BRANCH_UNUSABLE,
            GateBlockReason.PREDECESSOR_VALIDATION_PENDING,
            GateBlockReason.PREDECESSOR_REVIEW_PENDING,
        }

    def test_stack_merged_opens_merge(self):
        dep = Dependency(issue_number=20, mode=DependencyMode.STACK, state=DependencyState.UNSATISFIED)
        facts = {20: PredecessorFacts(branch_usable=True, validation_passed=True, agent_reviewed=True, merged=True)}
        report = build_gate_report(1, [dep], facts)
        assert report.all_open

    def test_stale_approval_reblocks_only_merge(self):
        dep = Dependency(issue_number=20, mode=DependencyMode.STACK, state=DependencyState.UNSATISFIED)
        facts = {20: PredecessorFacts(branch_usable=True, validation_passed=True, agent_reviewed=True, merged=True)}
        report = build_gate_report(1, [dep], facts, approval_current=False)
        assert report.can_start_work
        assert report.can_review
        assert report.can_publish
        assert not report.can_merge
        assert report.reason_codes(Gate.MERGE) == (GateBlockReason.APPROVAL_STALE,)

    def test_stack_satisfied_predecessor_opens_all(self):
        # A closed/merged predecessor fully satisfies the stack ordering.
        dep = Dependency(issue_number=20, mode=DependencyMode.STACK, state=DependencyState.SATISFIED)
        report = build_gate_report(1, [dep])
        assert report.all_open

    def test_base_branch_conflict_blocks_all_gates(self):
        dep = Dependency(issue_number=20, mode=DependencyMode.STACK, state=DependencyState.UNSATISFIED)
        facts = {20: PredecessorFacts(
            branch_usable=True, validation_passed=True, agent_reviewed=True, branch_name="feat-20"
        )}
        report = build_gate_report(1, [dep], facts, configured_base_branch="main")
        assert report.blocked_gates() == (Gate.WORK, Gate.REVIEW, Gate.PUBLISH, Gate.MERGE)
        assert report.reason_codes(Gate.WORK) == (GateBlockReason.BASE_BRANCH_CONFLICT,)

    def test_structural_problem_blocks_all_gates(self):
        dep = Dependency(
            issue_number=1, mode=DependencyMode.NORMAL,
            state=DependencyState.UNKNOWN, problem=EdgeProblem.SELF_DEPENDENCY,
        )
        report = build_gate_report(1, [dep])
        for gate in Gate:
            assert report.reason_codes(gate) == (GateBlockReason.SELF_DEPENDENCY,)

    @pytest.mark.parametrize(
        "state,expected",
        [
            (DependencyState.MISSING, GateBlockReason.DEPENDENCY_MISSING),
            (DependencyState.UNKNOWN, GateBlockReason.DEPENDENCY_UNKNOWN),
            (DependencyState.CROSS_MILESTONE, GateBlockReason.CROSS_MILESTONE),
        ],
    )
    def test_dependency_states_map_to_reason_codes(self, state, expected):
        dep = Dependency(issue_number=10, mode=DependencyMode.NORMAL, state=state)
        report = build_gate_report(1, [dep])
        assert report.reason_codes(Gate.WORK) == (expected,)


# --------------------------------------------------------------------------- #
# Evaluator integration
# --------------------------------------------------------------------------- #


class TestEvaluateGatesNormalEdges:
    def test_existing_depends_on_behavior_preserved(self, evaluator, checker):
        """A normal-only issue collapses every gate to the closed-only rule."""
        checker.add(10, "open")
        blocked = evaluator.evaluate_gates(1, "Depends-on: #10", "M1")
        assert blocked.blocked_gates() == (Gate.WORK, Gate.REVIEW, Gate.PUBLISH, Gate.MERGE)

        checker.add(10, "closed")
        ok = evaluator.evaluate_gates(1, "Depends-on: #10", "M1")
        assert ok.all_open

    def test_missing_predecessor_blocks_with_reason(self, evaluator):
        report = evaluator.evaluate_gates(1, "Depends-on: #999", "M1")
        assert report.reason_codes(Gate.WORK) == (GateBlockReason.DEPENDENCY_MISSING,)

    def test_unknown_on_transient_error(self, evaluator, checker):
        checker.add(10, "open")
        checker.error_on.add(10)
        report = evaluator.evaluate_gates(1, "Depends-on: #10", "M1")
        assert report.reason_codes(Gate.WORK) == (GateBlockReason.DEPENDENCY_UNKNOWN,)


class TestEvaluateGatesStackEdges:
    def test_stack_predecessor_missing_blocks(self, evaluator):
        report = evaluator.evaluate_gates(1, "Stack-after: #999", "M1")
        assert report.reason_codes(Gate.WORK) == (GateBlockReason.DEPENDENCY_MISSING,)

    def test_stack_open_predecessor_with_facts_unblocks_work(self, evaluator, checker):
        checker.add(20, "open")
        facts = {20: PredecessorFacts(branch_usable=True, validation_passed=True, agent_reviewed=True)}
        report = evaluator.evaluate_gates(1, "Stack-after: #20", "M1", predecessor_facts=facts)
        assert report.can_start_work
        assert not report.can_merge

    def test_stack_open_predecessor_without_facts_blocks_work(self, evaluator, checker):
        checker.add(20, "open")
        report = evaluator.evaluate_gates(1, "Stack-after: #20", "M1")
        assert not report.can_start_work
        assert GateBlockReason.PREDECESSOR_BRANCH_UNUSABLE in report.reason_codes(Gate.WORK)


class TestEvaluateGatesStructural:
    def test_self_dependency(self, evaluator):
        report = evaluator.evaluate_gates(100, "Depends-on: #100", "M1")
        assert report.reason_codes(Gate.WORK) == (GateBlockReason.SELF_DEPENDENCY,)

    def test_duplicate_declaration(self, evaluator, checker):
        checker.add(10, "open")
        report = evaluator.evaluate_gates(1, "Depends-on: #10\nDepends-on: #10", "M1")
        assert GateBlockReason.DUPLICATE_DECLARATION in report.reason_codes(Gate.WORK)

    def test_mode_conflict_same_target_normal_and_stack(self, evaluator, checker):
        checker.add(10, "open")
        report = evaluator.evaluate_gates(1, "Depends-on: #10\nStack-after: #10", "M1")
        assert GateBlockReason.MODE_CONFLICT in report.reason_codes(Gate.WORK)

    def test_malformed_reference(self, evaluator):
        report = evaluator.evaluate_gates(1, "Stack-after: gibberish", "M1")
        assert report.reason_codes(Gate.WORK) == (GateBlockReason.MALFORMED_REFERENCE,)

    def test_cycle_via_graph(self, evaluator, checker):
        checker.add(40, "open")
        report = evaluator.evaluate_gates(
            100, "Depends-on: #40", "M1", dependency_graph={100: [40], 40: [100]}
        )
        assert report.reason_codes(Gate.WORK) == (GateBlockReason.CYCLE,)


class TestEvaluateGatesCrossMilestone:
    def test_same_milestone_allowed(self, evaluator, checker):
        checker.add(10, "closed", milestone="M1")
        report = evaluator.evaluate_gates(1, "Depends-on: #10", "M1")
        assert report.all_open

    def test_foundation_milestone_allowed(self, evaluator, checker):
        checker.add(10, "closed", milestone="M0")
        report = evaluator.evaluate_gates(1, "Depends-on: #10", "M1")
        assert report.all_open

    def test_invalid_cross_milestone_blocks(self, evaluator, checker):
        checker.add(30, "open", milestone="M2")
        report = evaluator.evaluate_gates(1, "Depends-on: #30", "M1")
        assert report.reason_codes(Gate.WORK) == (GateBlockReason.CROSS_MILESTONE,)

    def test_stack_cross_milestone_without_membership_blocks(self, evaluator, checker):
        checker.add(30, "open", milestone="M2")
        report = evaluator.evaluate_gates(1, "Stack-after: #30", "M1")
        assert report.reason_codes(Gate.WORK) == (GateBlockReason.CROSS_MILESTONE,)

    def test_valid_same_stack_exception_allows_cross_milestone(self, evaluator, checker):
        checker.add(30, "open", milestone="M2")
        report = evaluator.evaluate_gates(
            1, "Stack-after: #30", "M1", same_stack_members=frozenset({30})
        )
        # Exception granted: no longer cross-milestone; now gated on stack facts.
        assert GateBlockReason.CROSS_MILESTONE not in report.reason_codes(Gate.WORK)
        assert GateBlockReason.PREDECESSOR_BRANCH_UNUSABLE in report.reason_codes(Gate.WORK)

    def test_same_stack_exception_does_not_relax_normal_edges(self, evaluator, checker):
        checker.add(30, "open", milestone="M2")
        report = evaluator.evaluate_gates(
            1, "Depends-on: #30", "M1", same_stack_members=frozenset({30})
        )
        assert report.reason_codes(Gate.WORK) == (GateBlockReason.CROSS_MILESTONE,)


class TestEvaluateGatesMixed:
    def test_mixed_normal_and_stack_independent(self, evaluator, checker):
        """A normal dep (closed) plus a stack predecessor (validated) -> work open."""
        checker.add(10, "closed")
        checker.add(20, "open")
        facts = {20: PredecessorFacts(branch_usable=True, validation_passed=True, agent_reviewed=True)}
        report = evaluator.evaluate_gates(
            1, "Depends-on: #10\nStack-after: #20", "M1", predecessor_facts=facts
        )
        assert report.can_start_work
        # Merge still ordered behind the stack predecessor.
        assert not report.can_merge
        assert report.reason_codes(Gate.MERGE) == (GateBlockReason.PREDECESSOR_NOT_MERGED,)


# --------------------------------------------------------------------------- #
# Downstream consumer decides from the gate report alone
# --------------------------------------------------------------------------- #


class TestDownstreamConsumer:
    def test_consumer_decides_work_without_inspecting_raw_dependencies(self, evaluator, checker):
        """A downstream caller routes purely through the gate API.

        It never reads dependency internals (state enums, edge modes); it asks
        the report whether work may start and reads machine-readable reasons.
        """

        def may_start_work(report) -> tuple[bool, list[str]]:
            return report.can_start_work, [c.value for c in report.reason_codes(Gate.WORK)]

        # Stack predecessor validated + reviewed + usable branch -> work allowed.
        checker.add(20, "open")
        facts = {20: PredecessorFacts(branch_usable=True, validation_passed=True, agent_reviewed=True)}
        ok_report = evaluator.evaluate_gates(1, "Stack-after: #20", "M1", predecessor_facts=facts)
        allowed, reasons = may_start_work(ok_report)
        assert allowed is True
        assert reasons == []

        # Predecessor not yet reviewed -> work blocked with a code the caller can act on.
        facts_pending = {20: PredecessorFacts(branch_usable=True, validation_passed=True, agent_reviewed=False)}
        blocked_report = evaluator.evaluate_gates(1, "Stack-after: #20", "M1", predecessor_facts=facts_pending)
        allowed, reasons = may_start_work(blocked_report)
        assert allowed is False
        assert "predecessor_review_pending" in reasons
