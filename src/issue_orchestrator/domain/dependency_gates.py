"""Dependency gate report (ADR-0029).

This module turns evaluated dependency edges plus git/PR facts into a single
**dependency gate report** that answers four lifecycle gates per dependent
slice:

| Gate    | Question                          |
|---------|-----------------------------------|
| work    | may agent work start?             |
| review  | may review start?                 |
| publish | may a PR be created/updated?      |
| merge   | may this slice merge?             |

A **normal** edge collapses all four gates to the existing rule: every gate is
blocked until the dependency issue is CLOSED. A **stack** edge unblocks *work*,
*review*, and *publish* once the predecessor exposes a usable, validated,
agent-reviewed branch, while *merge* stays ordered behind the predecessor's
merge. Reviewed-commit freshness for the slice itself (``approval_current``) is
a separate fact that only affects the *merge* gate.

The report is the single policy owner: scheduler, session launch, publish,
merge, recovery, and UI are intended to consume it rather than re-deriving stack
policy. Each blocked gate carries machine-readable :class:`GateBlockReason`
codes for diagnostics and downstream decisions.

This module is pure domain logic. The git/PR facts it consumes
(:class:`PredecessorFacts`) are gathered elsewhere and injected; this layer does
not perform any I/O.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from .dependencies import (
    Dependency,
    DependencyMode,
    DependencyState,
    DependencyTarget,
    EdgeProblem,
)


class Gate(Enum):
    """A lifecycle gate answered by the dependency gate report."""

    WORK = "work"
    REVIEW = "review"
    PUBLISH = "publish"
    MERGE = "merge"


class GateBlockReason(Enum):
    """Machine-readable reason a gate is blocked.

    Stable codes so downstream callers (and the UI) can branch on *why* a gate
    is closed without parsing human text.
    """

    # Normal-edge / shared dependency states
    DEPENDENCY_OPEN = "dependency_open"  # normal edge: dependency not yet closed
    DEPENDENCY_MISSING = "dependency_missing"  # dependency cannot be found
    DEPENDENCY_UNKNOWN = "dependency_unknown"  # transient error resolving state
    CROSS_MILESTONE = "cross_milestone"  # dependency violates milestone scope

    # Structural edge problems (the declared graph cannot be trusted)
    SELF_DEPENDENCY = "self_dependency"
    DUPLICATE_DECLARATION = "duplicate_declaration"
    MODE_CONFLICT = "mode_conflict"
    MALFORMED_REFERENCE = "malformed_reference"
    CYCLE = "cycle"
    BASE_BRANCH_CONFLICT = "base_branch_conflict"  # stack base conflicts with rule
    # More than one unmerged stack predecessor exposes a usable base branch, so
    # there is no single branch a linear successor can be based on. The base is
    # ambiguous and the gate must fail closed rather than fall back to the
    # default branch (ADR-0029: stack base has exactly one owner).
    AMBIGUOUS_STACK_BASE = "ambiguous_stack_base"

    # Stack-edge predecessor facts
    PREDECESSOR_BRANCH_UNUSABLE = "predecessor_branch_unusable"
    PREDECESSOR_VALIDATION_PENDING = "predecessor_validation_pending"
    PREDECESSOR_REVIEW_PENDING = "predecessor_review_pending"
    PREDECESSOR_NOT_MERGED = "predecessor_not_merged"
    # The successor no longer contains the predecessor branch head it was built
    # on (predecessor advanced / was force-pushed / reset / rebased). The
    # successor is stale and must not be publishable or merge-ready until it
    # contains the current predecessor head again.
    PREDECESSOR_BRANCH_ADVANCED = "predecessor_branch_advanced"

    # Slice's own reviewed-commit freshness
    APPROVAL_STALE = "approval_stale"


# Edge structural problem -> gate block reason (1:1 mapping).
_PROBLEM_REASON: dict[EdgeProblem, GateBlockReason] = {
    EdgeProblem.SELF_DEPENDENCY: GateBlockReason.SELF_DEPENDENCY,
    EdgeProblem.DUPLICATE_DECLARATION: GateBlockReason.DUPLICATE_DECLARATION,
    EdgeProblem.MODE_CONFLICT: GateBlockReason.MODE_CONFLICT,
    EdgeProblem.MALFORMED_REFERENCE: GateBlockReason.MALFORMED_REFERENCE,
    EdgeProblem.CYCLE: GateBlockReason.CYCLE,
}


@dataclass(frozen=True)
class PredecessorFacts:
    """Git/PR facts about a stack predecessor's branch head (ADR-0029 §2).

    These are *facts*, not labels: branch usability, validation, agent review,
    and merge state. They are gathered by the control/execution layer and
    injected into the gate report. Defaults are conservative (all False) so a
    stack predecessor with no observed facts keeps its dependent slice blocked
    until real facts arrive.
    """

    branch_usable: bool = False  # predecessor branch exists and is a usable base
    validation_passed: bool = False  # predecessor branch head is validation-green
    agent_reviewed: bool = False  # predecessor branch head has passed agent review
    merged: bool = False  # predecessor PR/branch has merged
    branch_name: str | None = None  # predecessor branch, for base reconciliation
    head_sha: str | None = None  # predecessor branch head commit, for staleness
    # Whether the dependent slice's successor branch currently contains this
    # predecessor's branch head (git ancestry). ``True`` is the conservative
    # default for gates that do not evaluate ancestry (the WORK gate runs before
    # a successor branch exists, so ancestry cannot apply there). The publish and
    # merge gates set this from a real ancestry check: when ``branch_usable`` but
    # ``not contained_in_successor`` the successor is stale and must not publish
    # or merge until it is rebuilt onto the current predecessor head.
    contained_in_successor: bool = True


@dataclass(frozen=True)
class GateBlock:
    """A single reason a gate is blocked, tied to a dependency reference."""

    reason: GateBlockReason
    dependency_ref: str
    detail: str | None = None


@dataclass(frozen=True)
class GateBlockRecord:
    """Flat, machine-readable record of one blocked-gate reason.

    A serialization-friendly projection of a :class:`GateBlock` enriched with the
    gate it blocks and the edge mode of its dependency, for events, logs, and
    skipped-reason payloads. ``mode`` is ``None`` only for blocks not tied to a
    resolved edge (e.g. the slice's own ``APPROVAL_STALE``).
    """

    gate: str
    predecessor: str
    reason: str
    mode: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "gate": self.gate,
            "predecessor": self.predecessor,
            "reason": self.reason,
            "mode": self.mode,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class GateDecision:
    """The decision for one gate: open or blocked, with reasons if blocked."""

    gate: Gate
    is_open: bool
    blocks: tuple[GateBlock, ...] = ()

    @property
    def blocked(self) -> bool:
        return not self.is_open

    @property
    def reason_codes(self) -> tuple[GateBlockReason, ...]:
        """De-duplicated reason codes (stable order) for this gate."""
        seen: list[GateBlockReason] = []
        for block in self.blocks:
            if block.reason not in seen:
                seen.append(block.reason)
        return tuple(seen)

    def summary(self) -> str:
        if self.is_open:
            return f"{self.gate.value}: open"
        codes = ", ".join(code.value for code in self.reason_codes)
        return f"{self.gate.value}: blocked ({codes})"


@dataclass(frozen=True)
class DependencyGateReport:
    """The single dependency policy report for a dependent slice.

    Exposes work/review/publish/merge gate decisions plus the slice's own
    ``approval_current`` fact. Downstream callers decide readiness from the
    public gate API (:meth:`can_start_work`, :meth:`gate`, ``reason_codes``)
    without inspecting raw dependency internals.
    """

    issue_number: int
    work: GateDecision
    review: GateDecision
    publish: GateDecision
    merge: GateDecision
    approval_current: bool = True
    dependencies: tuple[Dependency, ...] = field(default_factory=tuple)
    # The single owner of stack base selection (ADR-0029 / #6596): the branch a
    # stack successor's PR must be based on. It is the usable branch of the lone
    # unmerged stack predecessor, or ``None`` for a non-stack slice or one whose
    # stack predecessor has already merged (which should base on the normal
    # default branch). Publish wiring reads this instead of re-deriving the base.
    stack_base_branch: str | None = None

    def gate(self, gate: Gate) -> GateDecision:
        return {
            Gate.WORK: self.work,
            Gate.REVIEW: self.review,
            Gate.PUBLISH: self.publish,
            Gate.MERGE: self.merge,
        }[gate]

    @property
    def can_start_work(self) -> bool:
        return self.work.is_open

    @property
    def can_review(self) -> bool:
        return self.review.is_open

    @property
    def can_publish(self) -> bool:
        return self.publish.is_open

    @property
    def can_merge(self) -> bool:
        return self.merge.is_open

    @property
    def all_open(self) -> bool:
        return all(d.is_open for d in (self.work, self.review, self.publish, self.merge))

    def reason_codes(self, gate: Gate) -> tuple[GateBlockReason, ...]:
        return self.gate(gate).reason_codes

    def blocked_gates(self) -> tuple[Gate, ...]:
        return tuple(
            d.gate for d in (self.work, self.review, self.publish, self.merge) if d.blocked
        )

    def gate_block_records(self, gate: Gate) -> tuple[GateBlockRecord, ...]:
        """Machine-readable blocked-reason records for one gate.

        Each record carries the gate name, the offending predecessor reference,
        the edge mode (normal/stack), the machine-readable reason code, and any
        human detail — everything an event or skipped-reason payload needs to
        identify *which* dependency in *which* mode blocked *which* gate and
        *why*, without a consumer re-deriving it from raw dependency internals.
        """
        mode_by_ref = {dep.display_ref: dep.mode.value for dep in self.dependencies}
        return tuple(
            GateBlockRecord(
                gate=gate.value,
                predecessor=block.dependency_ref,
                reason=block.reason.value,
                mode=mode_by_ref.get(block.dependency_ref),
                detail=block.detail,
            )
            for block in self.gate(gate).blocks
        )

    def work_summary(self) -> str:
        """Human-readable WORK-gate status in the legacy dependency phrasing.

        For a slice whose work gate is open this matches
        :meth:`DependencyReport.summary` ("No dependencies" / "All N
        dependencies satisfied"). When blocked it groups the WORK-gate reasons
        into the same ``Blocked - waiting on: ...`` phrasing the legacy report
        used for normal edges (so existing diagnostics and the dashboard keep
        reading the same way), then appends any stack-predecessor or structural
        reasons by their machine code.
        """
        if self.work.is_open:
            if not self.dependencies:
                return "No dependencies"
            return f"All {len(self.dependencies)} dependencies satisfied"

        refs_by_reason: dict[GateBlockReason, list[str]] = {}
        for block in self.work.blocks:
            refs = refs_by_reason.setdefault(block.reason, [])
            if block.dependency_ref not in refs:
                refs.append(block.dependency_ref)

        parts: list[str] = []
        for reason in _LEGACY_WORK_PHRASE_ORDER:
            refs = refs_by_reason.pop(reason, None)
            if refs:
                parts.append(f"{_LEGACY_WORK_PHRASE[reason]}: {', '.join(refs)}")
        for reason, refs in refs_by_reason.items():
            parts.append(f"{reason.value}: {', '.join(refs)}")
        return "Blocked - " + "; ".join(parts)

    def dependency_state_counts(self) -> dict[DependencyState, int]:
        """Counts of this report's dependencies grouped by ``DependencyState``.

        Mirrors the partition the legacy :class:`DependencyReport` exposed
        (satisfied / unsatisfied / missing / unknown / cross-milestone) so the
        ``dependencies.evaluated`` event can keep emitting those count fields
        additively when the scheduler evaluates through the work gate, rather
        than dropping a catalogued, machine-consumable payload contract.
        """
        counts = {state: 0 for state in DependencyState}
        for dep in self.dependencies:
            counts[dep.state] += 1
        return counts

    def summary(self) -> str:
        return "; ".join(
            d.summary() for d in (self.work, self.review, self.publish, self.merge)
        )


# Legacy phrasing for the WORK-gate summary so a normal-edge block reads exactly
# as it did under ``DependencyReport.summary`` (consumed by the scheduler detail,
# the planner cross-milestone label rule, and the dashboard).
_LEGACY_WORK_PHRASE: dict[GateBlockReason, str] = {
    GateBlockReason.DEPENDENCY_OPEN: "waiting on",
    GateBlockReason.DEPENDENCY_MISSING: "missing",
    GateBlockReason.DEPENDENCY_UNKNOWN: "unknown",
    GateBlockReason.CROSS_MILESTONE: "cross-milestone",
}
_LEGACY_WORK_PHRASE_ORDER: tuple[GateBlockReason, ...] = (
    GateBlockReason.DEPENDENCY_OPEN,
    GateBlockReason.DEPENDENCY_MISSING,
    GateBlockReason.DEPENDENCY_UNKNOWN,
    GateBlockReason.CROSS_MILESTONE,
)


def _reaches_self(
    start: DependencyTarget, graph: Mapping[DependencyTarget, Sequence[DependencyTarget]]
) -> bool:
    """Whether ``start`` can reach itself by following edges (self-loops count)."""
    stack = list(graph.get(start, ()))
    seen: set[DependencyTarget] = set()
    while stack:
        node = stack.pop()
        if node == start:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, ()))
    return False


def detect_cycles(
    graph: Mapping[DependencyTarget, Sequence[DependencyTarget]],
) -> frozenset[DependencyTarget]:
    """Return the dependency targets that participate in a dependency cycle.

    ``graph`` maps a repository-aware :class:`DependencyTarget` to the targets
    it depends on. A node is in a cycle iff it can reach itself by following
    edges. Self-loops count. Keying by target identity (not bare issue number)
    keeps same-number cross-repo nodes distinct, so a real cycle is not
    fabricated from two unrelated targets that share a number.
    """
    return frozenset(node for node in graph if _reaches_self(node, graph))


# Per-gate block lists for a single edge, ordered (work, review, publish, merge).
_GateBlockLists = tuple[
    list[GateBlock], list[GateBlock], list[GateBlock], list[GateBlock]
]


def _block_all_gates(reason: GateBlockReason, ref: str, detail: str | None = None) -> _GateBlockLists:
    block = GateBlock(reason, ref, detail)
    return [block], [block], [block], [block]


def _stack_edge_blocks(
    dep: Dependency,
    ref: str,
    facts_by_target: Mapping[DependencyTarget, PredecessorFacts],
    configured_base_branch: str | None,
) -> _GateBlockLists:
    """Gate blocks contributed by an open stack predecessor edge."""
    facts = (
        facts_by_target.get(dep.target) if dep.target is not None else None
    ) or PredecessorFacts()

    # A stack base that conflicts with an issue-specific base-branch rule is a
    # validation failure, not something to silently resolve.
    if (
        configured_base_branch
        and facts.branch_name
        and configured_base_branch != facts.branch_name
    ):
        return _block_all_gates(
            GateBlockReason.BASE_BRANCH_CONFLICT,
            ref,
            f"configured base {configured_base_branch!r} conflicts with "
            f"predecessor branch {facts.branch_name!r}",
        )

    work: list[GateBlock] = []
    review: list[GateBlock] = []
    publish: list[GateBlock] = []
    merge: list[GateBlock] = []

    # review + publish + work all need a usable predecessor branch base.
    if not facts.branch_usable:
        branch_block = GateBlock(GateBlockReason.PREDECESSOR_BRANCH_UNUSABLE, ref)
        work.append(branch_block)
        review.append(branch_block)
        publish.append(branch_block)
    elif not facts.contained_in_successor:
        # The base branch exists but the successor no longer descends from the
        # current predecessor head (predecessor advanced / force-pushed / reset).
        # Ancestry is a publish/merge-readiness concern only: WORK already started
        # from some earlier head, and a first REVIEW is still launchable. Block
        # only publish and merge so the stale successor is rebuilt before it can
        # become a PR base point or merge.
        stale_block = GateBlock(
            GateBlockReason.PREDECESSOR_BRANCH_ADVANCED,
            ref,
            "successor branch does not contain the current predecessor head"
            + (f" {facts.head_sha}" if facts.head_sha else ""),
        )
        publish.append(stale_block)
        merge.append(stale_block)
    # work additionally needs validation + agent review of the predecessor head.
    if not facts.validation_passed:
        work.append(GateBlock(GateBlockReason.PREDECESSOR_VALIDATION_PENDING, ref))
    if not facts.agent_reviewed:
        work.append(GateBlock(GateBlockReason.PREDECESSOR_REVIEW_PENDING, ref))
    # merge stays ordered behind the predecessor's merge.
    if not facts.merged:
        merge.append(GateBlock(GateBlockReason.PREDECESSOR_NOT_MERGED, ref))

    return work, review, publish, merge


def _edge_gate_blocks(
    dep: Dependency,
    facts_by_target: Mapping[DependencyTarget, PredecessorFacts],
    configured_base_branch: str | None,
) -> _GateBlockLists:
    """Gate blocks contributed by a single evaluated dependency edge."""
    ref = dep.display_ref

    # Structural problems invalidate the whole graph -> block every gate.
    if dep.problem is not None:
        return _block_all_gates(_PROBLEM_REASON[dep.problem], ref, dep.error)

    blocking_states = {
        DependencyState.MISSING: GateBlockReason.DEPENDENCY_MISSING,
        DependencyState.UNKNOWN: GateBlockReason.DEPENDENCY_UNKNOWN,
        DependencyState.CROSS_MILESTONE: GateBlockReason.CROSS_MILESTONE,
    }
    if dep.state in blocking_states:
        return _block_all_gates(blocking_states[dep.state], ref, dep.error)

    if dep.mode == DependencyMode.NORMAL:
        if dep.state != DependencyState.SATISFIED:
            return _block_all_gates(GateBlockReason.DEPENDENCY_OPEN, ref)
        return [], [], [], []

    # --- Stack edge ---
    if dep.state == DependencyState.SATISFIED:
        # Predecessor merged/closed: fully satisfies the stack ordering.
        return [], [], [], []
    return _stack_edge_blocks(dep, ref, facts_by_target, configured_base_branch)


@dataclass(frozen=True)
class _StackBaseResolution:
    """Resolved stack base plus the edges that make it ambiguous, if any.

    ``base`` is the single branch a linear successor must be based on, or
    ``None`` when there is no live predecessor base (all merged → default
    branch) *or* when the base is ambiguous. ``ambiguous_edges`` is non-empty
    only in the ambiguous case (more than one usable unmerged predecessor branch)
    so the report can fail that case closed instead of falling back to default.
    """

    base: str | None
    ambiguous_edges: tuple[Dependency, ...] = ()
    branches: tuple[str, ...] = ()


def _resolve_stack_base(
    dependencies: Sequence[Dependency],
    facts_by_target: Mapping[DependencyTarget, PredecessorFacts],
) -> _StackBaseResolution:
    """Resolve the stack successor's base branch (ADR-0029, #6596).

    A linear stack has exactly one unmerged stack predecessor; its usable branch
    is the successor's base. A satisfied (merged/closed) stack predecessor
    contributes no base — the successor bases on the normal default branch. A
    predecessor whose PR has ``merged`` likewise contributes no base even while
    its dependency edge is still ``UNSATISFIED`` (its issue has not closed yet):
    the merge gate already treats ``facts.merged`` as unblocking, so basing the
    successor on the merged predecessor branch would target an already-merged
    branch instead of the default base. When no unmerged stack predecessor
    exposes a usable branch yet, the base is ``None`` (the per-edge
    ``PREDECESSOR_BRANCH_UNUSABLE`` block already keeps the gate closed). When
    *two or more* unmerged predecessors expose conflicting usable branches there
    is no single base, so this reports the conflicting edges and the owner fails
    the gate closed rather than guessing a default.
    """
    live: list[Dependency] = []
    branches: list[str] = []
    for dep in dependencies:
        if (
            dep.mode != DependencyMode.STACK
            or dep.problem is not None
            or dep.state != DependencyState.UNSATISFIED
            or dep.target is None
        ):
            continue
        facts = facts_by_target.get(dep.target)
        if facts and facts.branch_usable and facts.branch_name and not facts.merged:
            live.append(dep)
            branches.append(facts.branch_name)
    distinct = sorted(set(branches))
    if len(distinct) == 1:
        return _StackBaseResolution(base=distinct[0])
    if len(distinct) > 1:
        return _StackBaseResolution(
            base=None, ambiguous_edges=tuple(live), branches=tuple(distinct)
        )
    return _StackBaseResolution(base=None)


def build_gate_report(
    issue_number: int,
    dependencies: Sequence[Dependency],
    predecessor_facts: Mapping[DependencyTarget, PredecessorFacts] | None = None,
    *,
    approval_current: bool = True,
    configured_base_branch: str | None = None,
) -> DependencyGateReport:
    """Build the dependency gate report from evaluated edges and git/PR facts.

    Args:
        issue_number: The dependent slice being evaluated.
        dependencies: Evaluated edges, each carrying mode, state, and any
            structural problem. Normal edges gate on closure; stack edges gate
            on predecessor facts.
        predecessor_facts: Git/PR facts keyed by the predecessor's
            repository-aware :class:`DependencyTarget`. Keying by target (not a
            bare issue number) keeps a same-repo and a cross-repo predecessor
            with the same number from sharing facts. A stack predecessor with
            no entry is treated as having no usable branch yet (conservatively
            blocked).
        approval_current: Whether the slice's own reviewed commit is still its
            head. False re-blocks only the merge gate (``APPROVAL_STALE``).
        configured_base_branch: An issue-specific base-branch rule, if any. When
            it conflicts with a stack predecessor's branch the edge is reported
            as a base-branch validation failure rather than silently resolved.
    """
    facts_by_target = predecessor_facts or {}
    work: list[GateBlock] = []
    review: list[GateBlock] = []
    publish: list[GateBlock] = []
    merge: list[GateBlock] = []

    for dep in dependencies:
        w, r, p, m = _edge_gate_blocks(dep, facts_by_target, configured_base_branch)
        work += w
        review += r
        publish += p
        merge += m

    # Resolve the single stack base. When two or more unmerged predecessors
    # expose conflicting usable branches there is no safe base, so fail the
    # base-deciding gates closed instead of falling back to the default branch.
    base_resolution = _resolve_stack_base(dependencies, facts_by_target)
    if base_resolution.ambiguous_edges:
        detail = (
            "ambiguous stack base: unmerged predecessors expose conflicting "
            f"usable branches {list(base_resolution.branches)}"
        )
        for dep in base_resolution.ambiguous_edges:
            block = GateBlock(
                GateBlockReason.AMBIGUOUS_STACK_BASE, dep.display_ref, detail
            )
            # Block every gate that selects or builds on the base: WORK seeds the
            # successor worktree, PUBLISH picks the PR base, MERGE orders on it.
            work.append(block)
            publish.append(block)
            merge.append(block)

    # The slice's own reviewed-commit freshness only re-blocks merge.
    if not approval_current:
        merge.append(
            GateBlock(
                GateBlockReason.APPROVAL_STALE,
                f"#{issue_number}",
                "reviewed commit is no longer the slice head",
            )
        )

    return DependencyGateReport(
        issue_number=issue_number,
        work=GateDecision(Gate.WORK, is_open=not work, blocks=tuple(work)),
        review=GateDecision(Gate.REVIEW, is_open=not review, blocks=tuple(review)),
        publish=GateDecision(Gate.PUBLISH, is_open=not publish, blocks=tuple(publish)),
        merge=GateDecision(Gate.MERGE, is_open=not merge, blocks=tuple(merge)),
        approval_current=approval_current,
        dependencies=tuple(dependencies),
        stack_base_branch=base_resolution.base,
    )
