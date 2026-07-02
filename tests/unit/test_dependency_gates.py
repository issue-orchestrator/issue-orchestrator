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

from pathlib import Path

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
    ApprovalFreshness,
    Gate,
    GateBlockReason,
    PredecessorFacts,
    build_gate_report,
    detect_cycles,
)
from issue_orchestrator.ports import NullEventSink
from issue_orchestrator.ports.repository_host import DependencyIssueSnapshot


class MockIssueChecker:
    """Mock issue checker with per-issue state and milestone."""

    def __init__(self):
        self.state: dict[int, str] = {}
        self.milestone: dict[int, str | None] = {}
        self.error_on: set[int] = set()
        self.snapshot_calls: list[tuple[int, str | None]] = []

    def add(self, number: int, state: str, milestone: str | None = "M1"):
        self.state[number] = state
        self.milestone[number] = milestone

    def get_dependency_issue_snapshot(
        self, issue_number: int, repo: str | None = None
    ) -> DependencyIssueSnapshot | None:
        self.snapshot_calls.append((issue_number, repo))
        if issue_number in self.error_on:
            raise RuntimeError("API error")
        state = self.state.get(issue_number)
        if state is None:
            return None
        return DependencyIssueSnapshot(
            state=state,
            milestone=self.milestone.get(issue_number),
        )


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

    def test_unknown_approval_does_not_reblock_merge(self):
        # Tri-state: None means "no freshness source answered". Unlike False it
        # must NOT re-block merge (there is no evidence to block on) — it is
        # surfaced via approval_freshness instead of masquerading as fresh.
        dep = Dependency(issue_number=20, mode=DependencyMode.STACK, state=DependencyState.SATISFIED)
        report = build_gate_report(1, [dep], approval_current=None)
        assert report.can_merge
        assert GateBlockReason.APPROVAL_STALE not in report.reason_codes(Gate.MERGE)
        assert report.approval_freshness is ApprovalFreshness.UNKNOWN

    def test_approval_freshness_maps_the_tri_state_flag(self):
        dep = Dependency(issue_number=20, mode=DependencyMode.STACK, state=DependencyState.SATISFIED)
        assert build_gate_report(1, [dep], approval_current=True).approval_freshness is ApprovalFreshness.FRESH
        assert build_gate_report(1, [dep], approval_current=False).approval_freshness is ApprovalFreshness.STALE
        assert build_gate_report(1, [dep], approval_current=None).approval_freshness is ApprovalFreshness.UNKNOWN

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


class TestStackAncestryStaleness:
    """#6596: a successor that no longer contains the predecessor head is stale.

    Ancestry is a publish/merge-readiness concern. A predecessor that advances
    (force-push, reset for rework, rebase) while the successor still points at an
    older head must re-block publish and merge — but not retroactively block the
    work that already started or a first review launch.
    """

    def _stack_dep(self):
        return Dependency(
            issue_number=20, mode=DependencyMode.STACK, state=DependencyState.UNSATISFIED
        )

    def test_stale_successor_reblocks_publish_and_merge_only(self):
        facts = {
            DependencyTarget(20): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=True,
                branch_name="20-base", head_sha="deadbeef",
                contained_in_successor=False,
            )
        }
        report = build_gate_report(1, [self._stack_dep()], facts)
        assert report.can_start_work
        assert report.can_review
        assert not report.can_publish
        assert not report.can_merge
        assert GateBlockReason.PREDECESSOR_BRANCH_ADVANCED in report.reason_codes(Gate.PUBLISH)
        assert GateBlockReason.PREDECESSOR_BRANCH_ADVANCED in report.reason_codes(Gate.MERGE)

    def test_contained_successor_does_not_block_publish(self):
        facts = {
            DependencyTarget(20): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=True,
                branch_name="20-base", contained_in_successor=True,
            )
        }
        report = build_gate_report(1, [self._stack_dep()], facts)
        assert report.can_publish
        # merge still ordered behind the predecessor's own merge
        assert report.reason_codes(Gate.MERGE) == (GateBlockReason.PREDECESSOR_NOT_MERGED,)

    def test_unusable_branch_takes_precedence_over_ancestry(self):
        # No usable branch at all: report the unusable-branch reason, not a
        # spurious "advanced" on a branch that does not exist.
        facts = {
            DependencyTarget(20): PredecessorFacts(
                branch_usable=False, contained_in_successor=False
            )
        }
        report = build_gate_report(1, [self._stack_dep()], facts)
        assert GateBlockReason.PREDECESSOR_BRANCH_UNUSABLE in report.reason_codes(Gate.PUBLISH)
        assert GateBlockReason.PREDECESSOR_BRANCH_ADVANCED not in report.reason_codes(Gate.PUBLISH)

    def test_head_sha_appears_in_block_detail(self):
        facts = {
            DependencyTarget(20): PredecessorFacts(
                branch_usable=True, branch_name="20-base", head_sha="abc123",
                contained_in_successor=False,
            )
        }
        report = build_gate_report(1, [self._stack_dep()], facts)
        details = [b.detail for b in report.publish.blocks if b.detail]
        assert any("abc123" in d for d in details)


class TestStackBaseBranchSelection:
    """#6596: the report is the single owner of stack PR base selection."""

    def _stack_dep(self, state=DependencyState.UNSATISFIED):
        return Dependency(issue_number=20, mode=DependencyMode.STACK, state=state)

    def test_unmerged_predecessor_with_usable_branch_is_the_base(self):
        facts = {
            DependencyTarget(20): PredecessorFacts(
                branch_usable=True, branch_name="20-base"
            )
        }
        report = build_gate_report(1, [self._stack_dep()], facts)
        assert report.stack_base_branch == "20-base"

    def test_no_stack_edge_has_no_base(self):
        dep = Dependency(issue_number=10, mode=DependencyMode.NORMAL, state=DependencyState.UNSATISFIED)
        report = build_gate_report(1, [dep])
        assert report.stack_base_branch is None

    def test_merged_predecessor_bases_on_default_branch(self):
        # A satisfied (merged/closed) stack predecessor needs no stacked base.
        report = build_gate_report(1, [self._stack_dep(state=DependencyState.SATISFIED)])
        assert report.stack_base_branch is None

    def test_merged_facts_on_open_edge_do_not_select_merged_branch(self):
        # The predecessor PR merged before its issue closed: the edge is still
        # UNSATISFIED but facts.merged=True. The merge gate treats merged as
        # unblocking, so the base must NOT be the already-merged predecessor
        # branch -- the successor bases on the default branch instead.
        facts = {
            DependencyTarget(20): PredecessorFacts(
                branch_usable=True,
                validation_passed=True,
                agent_reviewed=True,
                merged=True,
                branch_name="20-base",
            )
        }
        report = build_gate_report(1, [self._stack_dep()], facts)
        assert report.stack_base_branch is None
        assert report.can_merge  # merged predecessor unblocks the merge gate

    def test_unusable_predecessor_branch_yields_no_base(self):
        report = build_gate_report(1, [self._stack_dep()])  # no facts
        assert report.stack_base_branch is None

    def test_conflicting_branches_fail_closed(self):
        # F4: two unmerged predecessors with distinct usable branches have no
        # single base. The owner must fail closed (no default-branch fallback)
        # rather than leave publish open with stack_base_branch=None.
        deps = [
            Dependency(issue_number=20, mode=DependencyMode.STACK, state=DependencyState.UNSATISFIED),
            Dependency(issue_number=21, mode=DependencyMode.STACK, state=DependencyState.UNSATISFIED),
        ]
        facts = {
            DependencyTarget(20): PredecessorFacts(branch_usable=True, branch_name="20-base"),
            DependencyTarget(21): PredecessorFacts(branch_usable=True, branch_name="21-base"),
        }
        report = build_gate_report(1, deps, facts)
        assert report.stack_base_branch is None
        assert not report.can_publish
        assert not report.can_start_work
        assert not report.can_merge
        assert GateBlockReason.AMBIGUOUS_STACK_BASE in report.reason_codes(Gate.PUBLISH)
        assert GateBlockReason.AMBIGUOUS_STACK_BASE in report.reason_codes(Gate.WORK)
        # The conflicting branches appear in the human detail for diagnostics.
        details = [b.detail for b in report.publish.blocks if b.detail]
        assert any("20-base" in d and "21-base" in d for d in details)

    def test_one_live_predecessor_with_others_merged_is_not_ambiguous(self):
        # A single unmerged predecessor plus already-merged ones is linear: the
        # one live usable branch is the base, no ambiguity block.
        deps = [
            Dependency(issue_number=20, mode=DependencyMode.STACK, state=DependencyState.SATISFIED),
            Dependency(issue_number=21, mode=DependencyMode.STACK, state=DependencyState.UNSATISFIED),
        ]
        facts = {
            DependencyTarget(21): PredecessorFacts(branch_usable=True, branch_name="21-base"),
        }
        report = build_gate_report(1, deps, facts)
        assert report.stack_base_branch == "21-base"
        assert GateBlockReason.AMBIGUOUS_STACK_BASE not in report.reason_codes(Gate.PUBLISH)
        assert report.can_publish


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


# --------------------------------------------------------------------------- #
# evaluate_work_gate: the owner surface scheduler / launch / planner consume
# --------------------------------------------------------------------------- #


class _RecordingEventSink:
    """Event sink that records published events for assertions."""

    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


class _FakeFactsProvider:
    """Stack-predecessor facts provider returning canned facts per target."""

    def __init__(self, facts: dict[DependencyTarget, PredecessorFacts]):
        self._facts = facts
        self.calls: list[list[DependencyTarget]] = []

    def gather_facts(self, targets):
        self.calls.append(list(targets))
        return {t: self._facts[t] for t in targets if t in self._facts}


class TestEvaluateWorkGate:
    def test_normal_edge_matches_legacy_runnable(self, checker):
        """A normal open dependency blocks work exactly like the legacy report."""
        checker.add(100, "open")
        evaluator = DependencyEvaluator(issue_checker=checker, events=NullEventSink())

        report = evaluator.evaluate_work_gate(1, "Depends-on: #100", "M1")

        assert report.can_start_work is False
        assert report.work_summary() == "Blocked - waiting on: #100"

    def test_normal_edge_closed_opens_work(self, checker):
        checker.add(100, "closed")
        evaluator = DependencyEvaluator(issue_checker=checker, events=NullEventSink())

        report = evaluator.evaluate_work_gate(1, "Depends-on: #100", "M1")

        assert report.can_start_work is True
        assert report.work_summary() == "All 1 dependencies satisfied"

    def test_stack_edge_blocked_without_provider(self, checker):
        """A stack predecessor stays blocked when no facts can be gathered."""
        checker.add(20, "open")
        evaluator = DependencyEvaluator(issue_checker=checker, events=NullEventSink())

        report = evaluator.evaluate_work_gate(2, "Stack-after: #20", "M1")

        assert report.can_start_work is False
        codes = [c.value for c in report.reason_codes(Gate.WORK)]
        assert "predecessor_branch_unusable" in codes

    def test_stack_edge_unblocks_on_ready_predecessor(self, checker):
        """Validated + reviewed + usable predecessor branch opens the work gate."""
        checker.add(20, "open")
        provider = _FakeFactsProvider(
            {DependencyTarget(20): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=True,
                branch_name="20-base",
            )}
        )
        evaluator = DependencyEvaluator(
            issue_checker=checker, events=NullEventSink(),
            predecessor_facts_provider=provider,
        )

        report = evaluator.evaluate_work_gate(2, "Stack-after: #20", "M1")

        assert report.can_start_work is True
        # Provider was asked only for the unsatisfied stack predecessor.
        assert provider.calls == [[DependencyTarget(20)]]

    def test_stack_edge_blocked_on_missing_validation(self, checker):
        checker.add(20, "open")
        provider = _FakeFactsProvider(
            {DependencyTarget(20): PredecessorFacts(
                branch_usable=True, validation_passed=False, agent_reviewed=True,
            )}
        )
        evaluator = DependencyEvaluator(
            issue_checker=checker, events=NullEventSink(),
            predecessor_facts_provider=provider,
        )

        report = evaluator.evaluate_work_gate(2, "Stack-after: #20", "M1")

        assert report.can_start_work is False
        codes = [c.value for c in report.reason_codes(Gate.WORK)]
        assert "predecessor_validation_pending" in codes

    def test_provider_not_called_for_normal_edges(self, checker):
        """Normal Depends-on: edges never consult the stack facts provider."""
        checker.add(100, "open")
        provider = _FakeFactsProvider({})
        evaluator = DependencyEvaluator(
            issue_checker=checker, events=NullEventSink(),
            predecessor_facts_provider=provider,
        )

        evaluator.evaluate_work_gate(1, "Depends-on: #100", "M1")

        assert provider.calls == []

    def test_provider_not_called_for_satisfied_stack_edge(self, checker):
        """A merged/closed stack predecessor opens work without gathering facts."""
        checker.add(20, "closed")
        provider = _FakeFactsProvider({})
        evaluator = DependencyEvaluator(
            issue_checker=checker, events=NullEventSink(),
            predecessor_facts_provider=provider,
        )

        report = evaluator.evaluate_work_gate(2, "Stack-after: #20", "M1")

        assert report.can_start_work is True
        assert provider.calls == []

    def test_emits_event_with_machine_readable_blocked_reasons(self, checker):
        """The work-gate event identifies issue, mode, gate, predecessor, reason."""
        checker.add(20, "open")
        provider = _FakeFactsProvider(
            {DependencyTarget(20): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=False,
            )}
        )
        events = _RecordingEventSink()
        evaluator = DependencyEvaluator(
            issue_checker=checker, events=events,
            predecessor_facts_provider=provider,
        )

        evaluator.evaluate_work_gate(2, "Stack-after: #20", "M1")

        assert len(events.events) == 1
        data = events.events[0].data
        assert events.events[0].name == "dependencies.evaluated"
        assert data["issue_number"] == 2
        assert data["runnable"] is False
        assert data["gate"] == "work"
        assert data["blocked_reasons"] == [
            {
                "gate": "work",
                "predecessor": "#20",
                "reason": "predecessor_review_pending",
                "mode": "stack",
                "detail": None,
            }
        ]

    def test_event_keeps_legacy_dependency_count_fields(self, checker):
        """A normal dependency evaluated through the work-gate path still exposes
        the legacy ``dependencies.evaluated`` count fields (additive contract)."""
        checker.add(10, "closed")
        checker.add(20, "open")
        events = _RecordingEventSink()
        evaluator = DependencyEvaluator(issue_checker=checker, events=events)

        evaluator.evaluate_work_gate(
            1, "Depends-on: #10\nDepends-on: #20", "M1"
        )

        assert len(events.events) == 1
        data = events.events[0].data
        # Legacy DependencyReport count fields are preserved.
        assert data["satisfied_count"] == 1
        assert data["unsatisfied_count"] == 1
        assert data["missing_count"] == 0
        assert data["unknown_count"] == 0
        assert data["cross_milestone_count"] == 0
        # New work-gate fields remain present alongside the legacy ones.
        assert data["gate"] == "work"
        assert data["runnable"] is False

    def test_no_event_when_issue_has_no_dependencies(self, checker):
        events = _RecordingEventSink()
        evaluator = DependencyEvaluator(issue_checker=checker, events=events)

        report = evaluator.evaluate_work_gate(1, "Body with no dependency directives", "M1")

        assert report.can_start_work is True
        assert events.events == []

    def test_diagnostic_can_suppress_event(self, checker):
        checker.add(100, "open")
        events = _RecordingEventSink()
        evaluator = DependencyEvaluator(issue_checker=checker, events=events)

        evaluator.evaluate_work_gate(1, "Depends-on: #100", "M1", emit_event=False)

        assert events.events == []


class TestWorkSummaryRendering:
    """work_summary() preserves legacy phrasing so diagnostics read the same."""

    def test_missing_and_cross_milestone_phrasing(self, checker):
        checker.add(100, "open")  # unsatisfied -> "waiting on"
        # #200 missing (no state), #300 different milestone -> cross-milestone
        checker.add(300, "open", milestone="M9")
        evaluator = DependencyEvaluator(issue_checker=checker, events=NullEventSink())

        report = evaluator.evaluate_work_gate(
            1, "Depends-on: #100\nDepends-on: #200\nDepends-on: #300", "M1"
        )
        summary = report.work_summary()
        assert summary.startswith("Blocked - ")
        assert "waiting on: #100" in summary
        assert "missing: #200" in summary
        assert "cross-milestone: #300" in summary

    def test_records_expose_mode_and_predecessor(self, checker):
        checker.add(100, "open")
        evaluator = DependencyEvaluator(issue_checker=checker, events=NullEventSink())

        report = evaluator.evaluate_work_gate(1, "Depends-on: #100", "M1")
        records = report.gate_block_records(Gate.WORK)
        assert len(records) == 1
        assert records[0].mode == "normal"
        assert records[0].predecessor == "#100"
        assert records[0].gate == "work"
        assert records[0].reason == "dependency_open"


class _FakeAncestry:
    """Stack branch-ancestry double: contained unless a branch is marked stale."""

    def __init__(self, stale_branches: set[str] | None = None):
        self._stale = stale_branches or set()
        self.calls: list[tuple[str, str]] = []

    def successor_contains_predecessor(self, worktree, predecessor_branch) -> bool:
        self.calls.append((str(worktree), predecessor_branch))
        return predecessor_branch not in self._stale


class TestEvaluatePublishGate:
    """#6596: the publish gate owner sets the PR base and re-blocks stale stacks."""

    def _ready_provider(self, branch="20-base"):
        return _FakeFactsProvider(
            {DependencyTarget(20): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=True,
                branch_name=branch, head_sha="cafe1234",
            )}
        )

    def test_non_stack_issue_publish_open_and_no_stack_base(self, checker):
        checker.add(100, "closed")
        evaluator = DependencyEvaluator(issue_checker=checker, events=NullEventSink())

        report = evaluator.evaluate_publish_gate(1, "Depends-on: #100", "M1")

        assert report.can_publish is True
        assert report.stack_base_branch is None

    def test_stack_publish_open_selects_predecessor_branch_as_base(self, checker):
        checker.add(20, "open")
        evaluator = DependencyEvaluator(
            issue_checker=checker, events=NullEventSink(),
            predecessor_facts_provider=self._ready_provider(),
        )

        report = evaluator.evaluate_publish_gate(
            2, "Stack-after: #20", "M1", worktree=Path("/wt/2")
        )

        assert report.can_publish is True
        assert report.stack_base_branch == "20-base"

    def test_predecessor_advanced_reblocks_publish(self, checker):
        checker.add(20, "open")
        ancestry = _FakeAncestry(stale_branches={"20-base"})
        evaluator = DependencyEvaluator(
            issue_checker=checker, events=NullEventSink(),
            predecessor_facts_provider=self._ready_provider(),
            branch_ancestry=ancestry,
        )

        report = evaluator.evaluate_publish_gate(
            2, "Stack-after: #20", "M1", worktree=Path("/wt/2")
        )

        assert report.can_publish is False
        assert GateBlockReason.PREDECESSOR_BRANCH_ADVANCED in report.reason_codes(Gate.PUBLISH)
        assert ancestry.calls == [("/wt/2", "20-base")]

    def test_contained_successor_keeps_publish_open(self, checker):
        checker.add(20, "open")
        ancestry = _FakeAncestry()  # all contained
        evaluator = DependencyEvaluator(
            issue_checker=checker, events=NullEventSink(),
            predecessor_facts_provider=self._ready_provider(),
            branch_ancestry=ancestry,
        )

        report = evaluator.evaluate_publish_gate(
            2, "Stack-after: #20", "M1", worktree=Path("/wt/2")
        )

        assert report.can_publish is True
        assert report.stack_base_branch == "20-base"

    def test_incompatible_base_branch_metadata_blocks_publish(self, checker):
        checker.add(20, "open")
        evaluator = DependencyEvaluator(
            issue_checker=checker, events=NullEventSink(),
            predecessor_facts_provider=self._ready_provider(branch="20-base"),
        )

        # Issue declares base-branch release/9 but the stack predecessor lives on
        # 20-base -> ADR-0022 conflict, fail fast with a publish diagnostic.
        report = evaluator.evaluate_publish_gate(
            2, "Stack-after: #20", "M1",
            worktree=Path("/wt/2"), configured_base_branch="release/9",
        )

        assert report.can_publish is False
        assert GateBlockReason.BASE_BRANCH_CONFLICT in report.reason_codes(Gate.PUBLISH)

    def test_ancestry_not_consulted_without_worktree(self, checker):
        checker.add(20, "open")
        ancestry = _FakeAncestry(stale_branches={"20-base"})
        evaluator = DependencyEvaluator(
            issue_checker=checker, events=NullEventSink(),
            predecessor_facts_provider=self._ready_provider(),
            branch_ancestry=ancestry,
        )

        report = evaluator.evaluate_publish_gate(2, "Stack-after: #20", "M1")

        # No worktree -> ancestry defaults to contained, publish stays open.
        assert report.can_publish is True
        assert ancestry.calls == []


class TestEvaluateMergeGate:
    """#6596: merge stays strictly ordered behind the predecessor's own merge."""

    def test_stack_successor_blocked_until_predecessor_merges(self, checker):
        checker.add(20, "open")  # predecessor still open
        provider = _FakeFactsProvider(
            {DependencyTarget(20): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=True,
                branch_name="20-base",
            )}
        )
        evaluator = DependencyEvaluator(
            issue_checker=checker, events=NullEventSink(),
            predecessor_facts_provider=provider,
        )

        report = evaluator.evaluate_merge_gate(2, "Stack-after: #20", "M1")

        assert report.can_merge is False
        assert report.reason_codes(Gate.MERGE) == (GateBlockReason.PREDECESSOR_NOT_MERGED,)

    def test_merge_opens_once_predecessor_closed(self, checker):
        checker.add(20, "closed")  # predecessor merged/closed
        evaluator = DependencyEvaluator(issue_checker=checker, events=NullEventSink())

        report = evaluator.evaluate_merge_gate(2, "Stack-after: #20", "M1")

        assert report.can_merge is True

    def test_stale_approval_reblocks_merge(self, checker):
        checker.add(20, "closed")
        evaluator = DependencyEvaluator(issue_checker=checker, events=NullEventSink())

        report = evaluator.evaluate_merge_gate(
            2, "Stack-after: #20", "M1", approval_current=False
        )

        assert report.can_merge is False
        assert report.reason_codes(Gate.MERGE) == (GateBlockReason.APPROVAL_STALE,)

    def test_non_stack_merge_matches_closed_only_rule(self, checker):
        checker.add(100, "open")
        evaluator = DependencyEvaluator(issue_checker=checker, events=NullEventSink())

        blocked = evaluator.evaluate_merge_gate(1, "Depends-on: #100", "M1")
        assert blocked.can_merge is False

        checker.add(100, "closed")
        ok = evaluator.evaluate_merge_gate(1, "Depends-on: #100", "M1")
        assert ok.can_merge is True


class TestThreeIssueStackScenario:
    """#6596 integration: a 3-issue stack C -> B -> A across work, publish base
    selection, stale detection, and the ordered merge gate, all decided by the
    single gate-report owner.

    A (#10) is the root; B (#20) stacks after A; C (#30) stacks after B. The
    fixtures simulate the facts each predecessor exposes at successive lifecycle
    stages.
    """

    def _evaluator(self, checker, facts, ancestry=None):
        return DependencyEvaluator(
            issue_checker=checker, events=NullEventSink(),
            predecessor_facts_provider=_FakeFactsProvider(facts),
            branch_ancestry=ancestry,
        )

    def test_b_work_unblocks_on_a_branch_then_c_waits_on_b(self, checker):
        # A is open but has a usable validated+reviewed branch; B is open with
        # only a usable branch (not yet reviewed) so C cannot start.
        checker.add(10, "open")
        checker.add(20, "open")
        facts = {
            DependencyTarget(10): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=True,
                branch_name="10-a",
            ),
            DependencyTarget(20): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=False,
                branch_name="20-b",
            ),
        }
        evaluator = self._evaluator(checker, facts)

        b = evaluator.evaluate_work_gate(20, "Stack-after: #10", "M1")
        c = evaluator.evaluate_work_gate(30, "Stack-after: #20", "M1")

        assert b.can_start_work is True       # A is ready -> B may start
        assert c.can_start_work is False      # B not reviewed yet -> C waits
        assert GateBlockReason.PREDECESSOR_REVIEW_PENDING in c.reason_codes(Gate.WORK)

    def test_b_publishes_on_a_branch_and_merge_waits_for_a(self, checker):
        checker.add(10, "open")  # A not merged yet
        facts = {
            DependencyTarget(10): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=True,
                branch_name="10-a",
            )
        }
        evaluator = self._evaluator(checker, facts, ancestry=_FakeAncestry())

        publish = evaluator.evaluate_publish_gate(
            20, "Stack-after: #10", "M1", worktree=Path("/wt/20")
        )
        merge = evaluator.evaluate_merge_gate(20, "Stack-after: #10", "M1")

        assert publish.can_publish is True
        assert publish.stack_base_branch == "10-a"   # B's PR bases on A's branch
        assert merge.can_merge is False              # ordered behind A
        assert merge.reason_codes(Gate.MERGE) == (GateBlockReason.PREDECESSOR_NOT_MERGED,)

    def test_a_force_push_makes_b_stale_for_publish(self, checker):
        checker.add(10, "open")
        facts = {
            DependencyTarget(10): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=True,
                branch_name="10-a", head_sha="newhead",
            )
        }
        # A force-pushed: B no longer contains 10-a's head.
        evaluator = self._evaluator(
            checker, facts, ancestry=_FakeAncestry(stale_branches={"10-a"})
        )

        publish = evaluator.evaluate_publish_gate(
            20, "Stack-after: #10", "M1", worktree=Path("/wt/20")
        )

        assert publish.can_publish is False
        assert GateBlockReason.PREDECESSOR_BRANCH_ADVANCED in publish.reason_codes(Gate.PUBLISH)

    def test_a_merged_opens_b_merge_and_c_still_waits_on_b(self, checker):
        # A merged (closed); B's stack edge is now satisfied -> B may merge.
        checker.add(10, "closed")
        checker.add(20, "open")  # B not merged yet
        facts = {
            DependencyTarget(20): PredecessorFacts(
                branch_usable=True, validation_passed=True, agent_reviewed=True,
                branch_name="20-b",
            )
        }
        evaluator = self._evaluator(checker, facts)

        b_merge = evaluator.evaluate_merge_gate(20, "Stack-after: #10", "M1")
        c_merge = evaluator.evaluate_merge_gate(30, "Stack-after: #20", "M1")

        assert b_merge.can_merge is True               # A merged -> B unblocked
        assert c_merge.can_merge is False              # B still open -> C waits
        assert c_merge.reason_codes(Gate.MERGE) == (GateBlockReason.PREDECESSOR_NOT_MERGED,)
