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
    DependencyTarget,
    EdgeProblem,
    parse_dependency_edges,
    parse_dependency_refs,
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

    def test_normal_edge_tolerates_inline_comment(self):
        """A documented inline ``# comment`` after a normal ref is tolerated.

        The legacy ``Depends-on:`` parser already ignores trailing text; the
        typed path must not regress that into ``MALFORMED_REFERENCE``.
        """
        edges = parse_dependency_edges("Depends-on: #123  # GitHub issue #123")
        assert len(edges) == 1
        assert edges[0].mode is DependencyMode.NORMAL
        assert edges[0].issue_number == 123
        assert edges[0].problem is None

    def test_stack_edge_tolerates_inline_comment(self):
        """Trailing text after a valid stack ref is tolerated too (one grammar)."""
        edges = parse_dependency_edges("Stack-after: #20  # base branch")
        assert len(edges) == 1
        assert edges[0].mode is DependencyMode.STACK
        assert edges[0].issue_number == 20
        assert edges[0].problem is None

    def test_malformed_normal_line_is_ignored_like_legacy(self):
        """An unparseable normal line is silently ignored, as the legacy path does.

        ``Depends-on: not a number`` and ``Depends-on: 123`` (no ``#``) are
        documented "silently ignored" mistakes (FAQ Q23). The typed normal path
        must preserve that — not surface them as malformed, gate-blocking edges.
        """
        assert parse_dependency_edges("Depends-on: not a number") == []
        assert parse_dependency_edges("Depends-on: 123") == []
        assert parse_dependency_edges("Depends-on: [M2-010]") == []


class TestNormalEdgeMatchesLegacyPath:
    """The typed normal edges must agree with the legacy reference parser.

    This is the anti-drift guarantee for F1/A1: ``parse_dependency_refs`` (the
    legacy owner used by ``evaluate``) and the normal-mode edges from
    ``parse_dependency_edges`` (used by ``evaluate_gates``) must resolve the same
    ``Depends-on:`` lines to the same identities — including the documented
    inline-comment examples and the documented silently-ignored mistakes.
    """

    @staticmethod
    def _normal_identities(body: str) -> list[tuple[int | None, str | None, str | None]]:
        edges = parse_dependency_edges(body)
        return [
            (e.issue_number, e.external_id, e.repository)
            for e in edges
            if e.mode is DependencyMode.NORMAL and e.problem is None
        ]

    @staticmethod
    def _legacy_identities(body: str) -> list[tuple[int | None, str | None, str | None]]:
        return [
            (r.issue_number, r.external_id, r.repository)
            for r in parse_dependency_refs(body)
        ]

    def test_documented_inline_comment_examples_agree(self):
        """The exact ``docs/user/faq.md`` Q22 examples parse identically."""
        body = (
            "Depends-on: #123                    # GitHub issue #123 (same milestone or M0)\n"
            "Depends-on: org/other-repo#456      # Cross-repo dependency\n"
            "Depends-on: M2-010                  # Issue with [M2-010] in its title\n"
        )
        assert self._normal_identities(body) == [
            (123, None, None),
            (456, None, "org/other-repo"),
            (None, "M2-010", None),
        ]
        assert self._normal_identities(body) == self._legacy_identities(body)

    def test_documented_ignored_mistakes_agree(self):
        """The ``docs/user/faq.md`` Q23 "silently ignored" lines yield nothing on both paths."""
        body = (
            "Depends-on: [M2-010]\n"
            "Depends-on: 123\n"
            "Depends-on: not a number\n"
        )
        assert self._normal_identities(body) == []
        assert self._legacy_identities(body) == []

    def test_leading_zero_issue_number_agrees(self):
        """``Depends-on: #010`` resolves to #10 on both paths (FAQ Q23)."""
        body = "Depends-on: #010"
        assert self._normal_identities(body) == [(10, None, None)]
        assert self._normal_identities(body) == self._legacy_identities(body)


# --------------------------------------------------------------------------- #
# Repository-aware target identity
# --------------------------------------------------------------------------- #


class TestDependencyTarget:
    def test_same_number_different_repo_are_distinct(self):
        assert DependencyTarget(20) != DependencyTarget(20, "other/repo")
        assert DependencyTarget(20, "a/b") != DependencyTarget(20, "c/d")

    def test_same_identity_is_equal_and_hashes_together(self):
        assert DependencyTarget(20, "a/b") == DependencyTarget(20, "a/b")
        assert len({DependencyTarget(20, "a/b"), DependencyTarget(20, "a/b")}) == 1

    def test_local_target_str(self):
        assert str(DependencyTarget(20)) == "#20"

    def test_cross_repo_target_str(self):
        assert str(DependencyTarget(20, "acme/widgets")) == "acme/widgets#20"

    def test_dependency_exposes_repository_aware_target(self):
        local = Dependency(issue_number=20, repository=None, mode=DependencyMode.STACK)
        cross = Dependency(issue_number=20, repository="acme/widgets", mode=DependencyMode.STACK)
        assert local.target == DependencyTarget(20)
        assert cross.target == DependencyTarget(20, "acme/widgets")
        assert local.target != cross.target

    def test_unresolved_dependency_has_no_target(self):
        # An unresolved external ID or malformed edge carries no concrete number.
        assert Dependency(issue_number=None, external_id="M1-010").target is None


# --------------------------------------------------------------------------- #
# Cycle detection (pure)
# --------------------------------------------------------------------------- #


class TestDetectCycles:
    def test_simple_cycle(self):
        t1, t2, t3 = DependencyTarget(1), DependencyTarget(2), DependencyTarget(3)
        assert detect_cycles({t1: [t2], t2: [t3], t3: [t1]}) == frozenset({t1, t2, t3})

    def test_self_loop(self):
        t5 = DependencyTarget(5)
        assert detect_cycles({t5: [t5]}) == frozenset({t5})

    def test_acyclic(self):
        t1, t2, t3 = DependencyTarget(1), DependencyTarget(2), DependencyTarget(3)
        assert detect_cycles({t1: [t2], t2: [t3], t3: []}) == frozenset()

    def test_node_outside_cycle_excluded(self):
        t1, t2, t3, t4 = (DependencyTarget(n) for n in (1, 2, 3, 4))
        # 4 -> 3 feeds into the 1-2-3 cycle but is not itself cyclic.
        assert detect_cycles({t1: [t2], t2: [t3], t3: [t1], t4: [t3]}) == frozenset({t1, t2, t3})

    def test_same_number_cross_repo_targets_are_distinct_nodes(self):
        # #1 (local) and other/repo#1 are different identities: an edge between
        # them is acyclic, not a fabricated self-cycle.
        local = DependencyTarget(1)
        cross = DependencyTarget(1, "other/repo")
        assert detect_cycles({local: [cross], cross: []}) == frozenset()


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
        facts = {DependencyTarget(20): PredecessorFacts(branch_usable=True, validation_passed=True, agent_reviewed=True)}
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
        facts = {DependencyTarget(20): PredecessorFacts(branch_usable=True, validation_passed=True, agent_reviewed=True, merged=True)}
        report = build_gate_report(1, [dep], facts)
        assert report.all_open

    def test_stale_approval_reblocks_only_merge(self):
        dep = Dependency(issue_number=20, mode=DependencyMode.STACK, state=DependencyState.UNSATISFIED)
        facts = {DependencyTarget(20): PredecessorFacts(branch_usable=True, validation_passed=True, agent_reviewed=True, merged=True)}
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
        facts = {DependencyTarget(20): PredecessorFacts(
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

    def test_facts_keyed_by_repository_aware_target_do_not_collide(self):
        """Two same-number stack predecessors in different repos stay distinct.

        ``#20`` (local) carries ready facts; ``other/repo#20`` carries none.
        Because facts are keyed by :class:`DependencyTarget`, the local edge's
        facts do not satisfy the cross-repo edge, so work stays blocked on the
        cross-repo predecessor's unusable branch.
        """
        local = Dependency(
            issue_number=20, repository=None,
            mode=DependencyMode.STACK, state=DependencyState.UNSATISFIED,
        )
        cross = Dependency(
            issue_number=20, repository="other/repo",
            mode=DependencyMode.STACK, state=DependencyState.UNSATISFIED,
        )
        facts = {
            DependencyTarget(20): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=True
            )
        }
        report = build_gate_report(1, [local, cross], facts)
        assert not report.can_start_work
        assert GateBlockReason.PREDECESSOR_BRANCH_UNUSABLE in report.reason_codes(Gate.WORK)


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

    def test_inline_comment_normal_dependency_does_not_falsely_block(self, evaluator, checker):
        """A documented ``Depends-on: #N  # comment`` line gates normally, not as malformed.

        Regression for F1: the typed gate path used to require the whole value to
        be a bare reference, so a documented inline-comment line became
        ``MALFORMED_REFERENCE`` and blocked every gate even when the dependency
        was satisfied.
        """
        checker.add(10, "closed")
        report = evaluator.evaluate_gates(1, "Depends-on: #10  # GitHub issue #10", "M1")
        assert report.all_open
        assert GateBlockReason.MALFORMED_REFERENCE not in report.reason_codes(Gate.WORK)

    def test_unparseable_normal_line_is_ignored_not_malformed(self, evaluator):
        """An unparseable normal line produces no edge, so it blocks nothing.

        Preserves the legacy "silently ignored" contract on the gate path; only
        the new ``Stack-after:`` syntax surfaces malformed values as blocking.
        """
        report = evaluator.evaluate_gates(1, "Depends-on: not a number", "M1")
        assert report.all_open

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
        facts = {DependencyTarget(20): PredecessorFacts(branch_usable=True, validation_passed=True, agent_reviewed=True)}
        report = evaluator.evaluate_gates(1, "Stack-after: #20", "M1", predecessor_facts=facts)
        assert report.can_start_work
        assert not report.can_merge

    def test_stack_open_predecessor_without_facts_blocks_work(self, evaluator, checker):
        checker.add(20, "open")
        report = evaluator.evaluate_gates(1, "Stack-after: #20", "M1")
        assert not report.can_start_work
        assert GateBlockReason.PREDECESSOR_BRANCH_UNUSABLE in report.reason_codes(Gate.WORK)

    def test_same_number_cross_repo_stack_edges_do_not_share_facts(self, evaluator, checker):
        """Predecessor facts must not leak across the repository boundary.

        The slice stacks on both ``#20`` (local) and ``other/repo#20``. Only
        the local target carries ready facts. If facts were keyed by bare issue
        number they would also satisfy the cross-repo edge and wrongly open
        work; keyed by repository-aware target, the factless cross-repo
        predecessor keeps work blocked.
        """
        checker.add(20, "open")
        facts = {
            DependencyTarget(20): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=True
            )
        }
        report = evaluator.evaluate_gates(
            1,
            "Stack-after: #20\nStack-after: other/repo#20",
            "M1",
            predecessor_facts=facts,
        )
        assert not report.can_start_work
        assert GateBlockReason.PREDECESSOR_BRANCH_UNUSABLE in report.reason_codes(Gate.WORK)

    def test_cross_repo_stack_edge_uses_its_own_facts(self, evaluator, checker):
        """Facts keyed by the cross-repo target apply to the cross-repo edge.

        Complement of the leak test: when the ready facts are keyed by the
        cross-repo target, the cross-repo stack edge unblocks work — proving the
        identity match works in both directions.
        """
        checker.add(20, "open")
        facts = {
            DependencyTarget(20, "other/repo"): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=True
            )
        }
        report = evaluator.evaluate_gates(
            1, "Stack-after: other/repo#20", "M1", predecessor_facts=facts
        )
        assert report.can_start_work
        assert not report.can_merge


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
        t100, t40 = DependencyTarget(100), DependencyTarget(40)
        report = evaluator.evaluate_gates(
            100, "Depends-on: #40", "M1", dependency_graph={t100: [t40], t40: [t100]}
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
            1, "Stack-after: #30", "M1", same_stack_members=frozenset({DependencyTarget(30)})
        )
        # Exception granted: no longer cross-milestone; now gated on stack facts.
        assert GateBlockReason.CROSS_MILESTONE not in report.reason_codes(Gate.WORK)
        assert GateBlockReason.PREDECESSOR_BRANCH_UNUSABLE in report.reason_codes(Gate.WORK)

    def test_same_stack_exception_does_not_relax_normal_edges(self, evaluator, checker):
        checker.add(30, "open", milestone="M2")
        report = evaluator.evaluate_gates(
            1, "Depends-on: #30", "M1", same_stack_members=frozenset({DependencyTarget(30)})
        )
        assert report.reason_codes(Gate.WORK) == (GateBlockReason.CROSS_MILESTONE,)

    def test_same_stack_membership_respects_repository_identity(self, evaluator, checker):
        """A local same-stack member must not relax a cross-repo same-number edge.

        Membership is granted only to ``#30`` in the current repo. A
        ``Stack-after: other/repo#30`` edge resolves to a *different* target
        identity, so the bounded cross-milestone exception does not apply and
        the cross-milestone block stands.
        """
        checker.add(30, "open", milestone="M2")
        report = evaluator.evaluate_gates(
            1,
            "Stack-after: other/repo#30",
            "M1",
            same_stack_members=frozenset({DependencyTarget(30)}),
        )
        assert report.reason_codes(Gate.WORK) == (GateBlockReason.CROSS_MILESTONE,)

    def test_same_stack_membership_matches_cross_repo_target(self, evaluator, checker):
        """The exception is granted when the membership target's repo matches.

        Same body as above, but membership now names the cross-repo target, so
        the exception applies and the edge falls through to stack-fact gating.
        """
        checker.add(30, "open", milestone="M2")
        report = evaluator.evaluate_gates(
            1,
            "Stack-after: other/repo#30",
            "M1",
            same_stack_members=frozenset({DependencyTarget(30, "other/repo")}),
        )
        assert GateBlockReason.CROSS_MILESTONE not in report.reason_codes(Gate.WORK)
        assert GateBlockReason.PREDECESSOR_BRANCH_UNUSABLE in report.reason_codes(Gate.WORK)


class TestEvaluateGatesMixed:
    def test_mixed_normal_and_stack_independent(self, evaluator, checker):
        """A normal dep (closed) plus a stack predecessor (validated) -> work open."""
        checker.add(10, "closed")
        checker.add(20, "open")
        facts = {DependencyTarget(20): PredecessorFacts(branch_usable=True, validation_passed=True, agent_reviewed=True)}
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
        facts = {DependencyTarget(20): PredecessorFacts(branch_usable=True, validation_passed=True, agent_reviewed=True)}
        ok_report = evaluator.evaluate_gates(1, "Stack-after: #20", "M1", predecessor_facts=facts)
        allowed, reasons = may_start_work(ok_report)
        assert allowed is True
        assert reasons == []

        # Predecessor not yet reviewed -> work blocked with a code the caller can act on.
        facts_pending = {DependencyTarget(20): PredecessorFacts(branch_usable=True, validation_passed=True, agent_reviewed=False)}
        blocked_report = evaluator.evaluate_gates(1, "Stack-after: #20", "M1", predecessor_facts=facts_pending)
        allowed, reasons = may_start_work(blocked_report)
        assert allowed is False
        assert "predecessor_review_pending" in reasons
