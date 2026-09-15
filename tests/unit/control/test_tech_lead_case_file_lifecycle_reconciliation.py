"""Reviewed backlog lifecycle reconciliation tests (#7240)."""

from __future__ import annotations

import hashlib
from typing import Any, cast

import pytest

from issue_orchestrator.control.pattern_registry import LocalPatternCaseFileRegistry
from issue_orchestrator.control.mutation_gate import ReconciliationGate
from issue_orchestrator.control.tech_lead_case_file_lifecycle_reconciliation import (
    CaseFileLifecycleReconciler,
    CaseFileLifecycleReconciliationPlan,
    CaseFileLifecycleReconciliationRefused,
    GatedCaseFileMutationAuthority,
)
from issue_orchestrator.control.tech_lead_case_file_lifecycle import (
    retirement_comment,
)
from issue_orchestrator.domain.tech_lead_findings import CaseFileLifecycleTransition
from issue_orchestrator.control.reconciliation import ReconciliationRequired
from issue_orchestrator.ports.comment_receipt import IssueCommentReceipt
from issue_orchestrator.ports.fresh_issue_reader import (
    FreshIssueReadError,
    FreshIssueSnapshot,
)
from issue_orchestrator.ports.pattern_registry import PatternRegistryError
from issue_orchestrator.ports.tech_lead_authority import (
    InMemoryTechLeadAuthorityStore,
)


def _plan(*outcomes: dict[str, object]) -> CaseFileLifecycleReconciliationPlan:
    return CaseFileLifecycleReconciliationPlan.from_mapping(
        {
            "plan_id": "case-files-2026-09",
            "repository": "owner/repo",
            "recorded_at": "2026-09-10T12:00:00+00:00",
            "outcomes": list(outcomes),
        }
    )


def _outcome(
    signature: str,
    issue: int,
    disposition: str,
    *,
    expected_revision: str = "0" * 64,
) -> dict[str, object]:
    return {
        "signature": signature,
        "issue": issue,
        "disposition": disposition,
        "expected_revision": expected_revision,
        "reason": f"Reviewed outcome for {signature}.",
        "evidence": [f"owner/repo#{issue}"],
    }


class _Repository:
    def __init__(self) -> None:
        self.comments: list[tuple[int, str]] = []
        self.closed: list[int] = []

    def add_comment(self, issue: int, body: str) -> str:
        self.comments.append((issue, body))
        return "comment"

    def find_issue_comment_receipt(
        self, issue: int, *, body: str
    ) -> IssueCommentReceipt | None:
        if (issue, body) not in self.comments:
            return None
        return IssueCommentReceipt(
            comment_id="1",
            url="comment",
            author_key="app:test",
            body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        )

    def update_issue_state(self, issue: int, state: str) -> None:
        assert state == "closed"
        self.closed.append(issue)


class _Authority:
    """Records the KIND of authority each write asked for."""

    def __init__(self, on_require=lambda _issue: None) -> None:
        self.calls: list[tuple[str, int]] = []
        self._on_require = on_require

    def require_retirement(self, issue_number: int) -> None:
        self.calls.append(("retirement", issue_number))
        self._on_require(issue_number)

    def require_classification(self, issue_number: int) -> None:
        self.calls.append(("classification", issue_number))
        self._on_require(issue_number)


def _reconciler(
    *rows: tuple[str, int],
    mutation_authority=None,
):
    authority = InMemoryTechLeadAuthorityStore()
    for signature, issue in rows:
        authority.record_pattern(
            signature=signature,
            issue_number=issue,
            observation_id=f"seed:{signature}",
        )
    registry = LocalPatternCaseFileRegistry(authority)
    repository = _Repository()
    return (
        CaseFileLifecycleReconciler(
            registry=registry,
            repository_host=cast(Any, repository),
            mutation_authority=mutation_authority or _Authority(),
        ),
        registry,
        repository,
    )


def _reviewed_outcome(registry, signature: str, issue: int, disposition: str):
    entry = registry.read(signature=signature)
    assert entry is not None
    return _outcome(
        signature,
        issue,
        disposition,
        expected_revision=entry.review_revision(),
    )


def test_plan_rejects_unknown_fields_duplicate_identity_and_bad_disposition() -> None:
    with pytest.raises(ValueError, match="unknown keys"):
        CaseFileLifecycleReconciliationPlan.from_mapping(
            {
                "plan_id": "x",
                "repository": "owner/repo",
                "recorded_at": "2026-09-10T12:00:00+00:00",
                "outcomes": [_outcome("a", 1, "active")],
                "surprise": True,
            }
        )
    with pytest.raises(ValueError, match="repeat a signature"):
        _plan(_outcome("a", 1, "active"), _outcome("a", 2, "active"))
    with pytest.raises(ValueError, match="not one of"):
        _plan(_outcome("a", 1, "old"))


def test_dry_run_requires_exact_repository_registry_coverage_and_writes_nothing() -> (
    None
):
    reconciler, registry, repository = _reconciler(("a", 1), ("b", 2))
    incomplete = _plan(_reviewed_outcome(registry, "a", 1, "active"))
    with pytest.raises(ValueError, match=r"missing=\['b'\]"):
        reconciler.run(
            incomplete, configured_repository="owner/repo", apply_writes=False
        )
    complete = _plan(
        _reviewed_outcome(registry, "a", 1, "active"),
        _reviewed_outcome(registry, "b", 2, "needs_human"),
    )

    result = reconciler.run(
        complete, configured_repository="owner/repo", apply_writes=False
    )

    assert (result.active, result.needs_human, result.terminal) == (1, 1, 0)
    assert repository.comments == []
    assert repository.closed == []


def test_apply_records_nonterminal_and_comment_before_terminal_close() -> None:
    reconciler, registry, repository = _reconciler(("a", 1), ("b", 2))
    plan = _plan(
        _reviewed_outcome(registry, "a", 1, "needs_human"),
        _reviewed_outcome(registry, "b", 2, "superseded"),
    )

    result = reconciler.run(plan, configured_repository="OWNER/REPO", apply_writes=True)

    assert result.ok and result.applied == 2
    assert repository.closed == [2]
    assert [number for number, _body in repository.comments] == [2]
    assert registry.read(signature="a").disposition == "needs_human"  # type: ignore[union-attr]
    assert registry.read(signature="b").disposition == "superseded"  # type: ignore[union-attr]


def test_wrong_issue_mapping_and_repository_fail_before_writes() -> None:
    reconciler, registry, repository = _reconciler(("a", 1))
    reviewed = _reviewed_outcome(registry, "a", 1, "active")
    with pytest.raises(ValueError, match="could not be resolved"):
        reconciler.run(
            _plan(reviewed),
            configured_repository=None,
            apply_writes=True,
        )
    with pytest.raises(ValueError, match="this repository"):
        reconciler.run(
            _plan(reviewed),
            configured_repository="elsewhere/repo",
            apply_writes=True,
        )
    with pytest.raises(ValueError, match="registered to case file #1"):
        reconciler.run(
            _plan(
                _outcome(
                    "a",
                    99,
                    "invalid",
                    expected_revision=registry.read(signature="a").review_revision(),  # type: ignore[union-attr]
                )
            ),
            configured_repository="owner/repo",
            apply_writes=True,
        )
    assert repository.comments == []
    assert repository.closed == []


def test_changed_evidence_revision_invalidates_the_reviewed_plan() -> None:
    reconciler, registry, _repository = _reconciler(("a", 1))
    plan = _plan(_reviewed_outcome(registry, "a", 1, "shipped"))
    registry.record_lifecycle(
        signature="a",
        transition=CaseFileLifecycleTransition(
            transition_id="new-review",
            disposition="needs_human",
            reason="New evidence needs review.",
            evidence=("owner/repo#2",),
            recorded_at="2026-09-11T12:00:00+00:00",
        ),
    )

    with pytest.raises(ValueError, match="changed since lifecycle review"):
        reconciler.run(plan, configured_repository="owner/repo", apply_writes=False)


def test_mutation_guard_and_atomic_revision_check_precede_external_writes() -> None:
    calls: list[int] = []
    registry_holder = []

    def race(issue: int) -> None:
        calls.append(issue)
        registry = registry_holder[0]
        registry.record_lifecycle(
            signature="a",
            transition=CaseFileLifecycleTransition(
                transition_id="concurrent-review",
                disposition="needs_human",
                reason="Concurrent evidence arrived.",
                evidence=("owner/repo#3",),
                recorded_at="2026-09-11T13:00:00+00:00",
            ),
        )

    reconciler, registry, repository = _reconciler(
        ("a", 1), mutation_authority=_Authority(on_require=race)
    )
    registry_holder.append(registry)
    plan = _plan(_reviewed_outcome(registry, "a", 1, "shipped"))

    result = reconciler.run(plan, configured_repository="owner/repo", apply_writes=True)

    assert calls == [1]
    assert result.failures and "changed since lifecycle review" in result.failures[0]
    assert repository.comments == []
    assert repository.closed == []


def test_reconciliation_hold_fails_closed_before_retirement_writes() -> None:
    class _FreshLabels:
        def read_issue_labels(self, issue_number: int) -> list[str]:
            assert issue_number == 1
            return ["io:needs-reconcile"]

    gate = ReconciliationGate(fresh_issue_reader=_FreshLabels(), reconcile=True)
    reconciler, registry, repository = _reconciler(
        ("a", 1),
        mutation_authority=GatedCaseFileMutationAuthority(
            gate=gate, pause_label="io:needs-reconcile"
        ),
    )
    plan = _plan(_reviewed_outcome(registry, "a", 1, "shipped"))

    result = reconciler.run(plan, configured_repository="owner/repo", apply_writes=True)

    assert result.applied == 0
    assert result.failures and "Has forbidden labels" in result.failures[0]
    assert repository.comments == []
    assert repository.closed == []
    assert registry.read(signature="a").lifecycle == ()  # type: ignore[union-attr]


def test_preflight_refuses_a_plan_whose_later_row_is_inadmissible() -> None:
    """The WHOLE plan is admitted before any of it is applied.

    Preflight used to ask a narrower question than the write path: a private
    copy of the successful-replay half of ``admit_lifecycle_transition``. A
    signature already terminal under a DIFFERENT transition therefore passed
    preflight whenever its ``expected_revision`` still matched, and the registry
    only refused it in the middle of the apply — after the earlier rows had
    already commented on and closed their case files. A reviewed plan is one
    decision set, so an inadmissible row anywhere in it must stop the command
    before its first write (#7248 review F2/A2).
    """
    guarded = _Authority()
    reconciler, registry, repository = _reconciler(
        ("a", 1), ("b", 2), mutation_authority=guarded
    )
    # Retire "b" under a different transition BEFORE the plan is reviewed, so
    # its expected_revision matches the terminal state exactly. The staleness
    # rule therefore has nothing to object to, and admission is the only rule
    # left that can refuse this row.
    _retire(registry, "b", 2)
    plan = _plan(
        _reviewed_outcome(registry, "a", 1, "shipped"),
        _reviewed_outcome(registry, "b", 2, "superseded"),
    )

    with pytest.raises(CaseFileLifecycleReconciliationRefused, match="terminal"):
        reconciler.run(plan, configured_repository="owner/repo", apply_writes=True)

    assert guarded.calls == []
    assert repository.comments == []
    assert repository.closed == []
    assert registry.read(signature="a").lifecycle == ()  # type: ignore[union-attr]


def test_preflight_refuses_a_transition_identity_reused_with_a_new_payload() -> None:
    """One reviewed intent per transition identity, checked for the whole plan.

    The second half of the admission rule preflight was skipping: a
    ``transition_id`` already recorded with a DIFFERENT payload is not a replay
    and must never be admitted, whatever the reviewed revision says. Because the
    identity is derived from the plan id and signature, this is what a plan
    re-authored under its old id looks like (#7248 review F2).
    """
    guarded = _Authority()
    reconciler, registry, repository = _reconciler(
        ("a", 1), ("b", 2), mutation_authority=guarded
    )
    registry.record_lifecycle(
        signature="b",
        transition=CaseFileLifecycleTransition(
            transition_id="case-files-2026-09:b",
            disposition="needs_human",
            reason="The reason this plan id was first recorded with.",
            evidence=("owner/repo#2",),
            recorded_at="2026-09-10T12:00:00+00:00",
        ),
    )
    plan = _plan(
        _reviewed_outcome(registry, "a", 1, "shipped"),
        _reviewed_outcome(registry, "b", 2, "active"),
    )

    with pytest.raises(
        CaseFileLifecycleReconciliationRefused, match="changed payload"
    ):
        reconciler.run(plan, configured_repository="owner/repo", apply_writes=True)

    assert guarded.calls == []
    assert repository.comments == []
    assert repository.closed == []
    assert registry.read(signature="a").lifecycle == ()  # type: ignore[union-attr]


def test_an_interrupted_retirement_of_the_same_intent_previews_and_resumes() -> None:
    """An in-flight terminal write of this plan's own intent is a resume.

    Its entry has no review revision to compare — a pending external effect
    means the settled facts are not yet knowable — so preflight must recognise
    the resume before it reaches the staleness rule, exactly as the registries
    do inside their reserving compare-and-swap. The pending retirement here
    carries the exact body ``retirement_comment`` renders, which is what makes
    it genuinely resumable: the apply that follows the dry run drives the same
    reservation through its comment and its close.
    """
    reconciler, registry, repository = _reconciler(("a", 1))
    plan = _plan(_reviewed_outcome(registry, "a", 1, "shipped"))
    transition = plan.outcomes[0].transition(
        plan_id=plan.plan_id, recorded_at=plan.recorded_at
    )
    registry.reserve_retirement(
        signature="a",
        transition=transition,
        comment=retirement_comment(transition),
        issue_number=1,
    )

    preview = reconciler.run(
        plan, configured_repository="owner/repo", apply_writes=False
    )
    assert preview.dry_run and preview.terminal == 1
    assert repository.comments == [] and repository.closed == []

    applied = reconciler.run(
        plan, configured_repository="owner/repo", apply_writes=True
    )

    assert applied.ok and applied.applied == 1
    assert repository.comments == [(1, retirement_comment(transition))]
    assert repository.closed == [1]
    assert registry.read(signature="a").disposition == "shipped"  # type: ignore[union-attr]


def test_preflight_refuses_a_pending_retirement_whose_comment_changed() -> None:
    """Same intent is not enough to inherit an interrupted retirement.

    A retirement's pending comment is the idempotency key its recovery path
    searches for, so both registries require the pending body to equal the one
    the apply would render. Preflight asked only about transition intent, so an
    entry it called resumable was one ``reserve_retirement`` would reject —
    and the rejection landed mid-apply, after the earlier row had already been
    commented on and closed (#7248 round 2 review F2/A2).
    """
    guarded = _Authority()
    reconciler, registry, repository = _reconciler(
        ("a", 1), ("b", 2), mutation_authority=guarded
    )
    plan = _plan(
        _reviewed_outcome(registry, "a", 1, "shipped"),
        _reviewed_outcome(registry, "b", 2, "superseded"),
    )
    registry.reserve_retirement(
        signature="b",
        transition=plan.outcomes[1].transition(
            plan_id=plan.plan_id, recorded_at=plan.recorded_at
        ),
        comment="<!-- a body an older build rendered -->",
        issue_number=2,
    )

    with pytest.raises(
        CaseFileLifecycleReconciliationRefused, match="retirement comment changed"
    ):
        reconciler.run(plan, configured_repository="owner/repo", apply_writes=True)

    assert guarded.calls == []
    assert repository.comments == []
    assert repository.closed == []
    assert registry.read(signature="a").lifecycle == ()  # type: ignore[union-attr]


def test_a_shared_registry_read_failure_is_a_bounded_refusal() -> None:
    """A live shared read fails for ordinary operational reasons.

    ``list_entries`` is a GitHub-ref read: transport, authentication, a CAS
    conflict, or a malformed registry all raise ``PatternRegistryError``, and
    validation performs that read AFTER composition, so the bootstrap read
    succeeding proves nothing. Untyped it escaped the supported CLI as a
    traceback instead of the nonzero, explained stop the command promises
    (#7248 review F3).
    """
    repository = _Repository()
    reconciler = CaseFileLifecycleReconciler(
        registry=cast(Any, _UnreadableRegistry()),
        repository_host=cast(Any, repository),
        mutation_authority=_Authority(),
    )

    with pytest.raises(
        CaseFileLifecycleReconciliationRefused,
        match="shared pattern authority could not be read",
    ):
        reconciler.run(
            _plan(_outcome("a", 1, "active")),
            configured_repository="owner/repo",
            apply_writes=True,
        )

    assert repository.comments == [] and repository.closed == []


def _retire(registry, signature: str, issue: int) -> None:
    """Drive one terminal transition to completion through the registry owner."""
    reserved = registry.reserve_retirement(
        signature=signature,
        transition=CaseFileLifecycleTransition(
            transition_id=f"already-terminal:{signature}",
            disposition="invalid",
            reason="Retired before this plan was applied.",
            evidence=(f"owner/repo#{issue}",),
            recorded_at="2026-09-09T13:00:00+00:00",
        ),
        comment="<!-- retirement -->",
        issue_number=issue,
    )
    registry.confirm_retirement_comment(
        signature=signature, reservation_id=reserved.entry.reservation_id
    )
    registry.finalize_retirement(
        signature=signature, reservation_id=reserved.entry.reservation_id
    )


class _UnreadableRegistry:
    """Shared authority whose live ref read fails the way GitHub's can."""

    def list_entries(self) -> tuple[Any, ...]:
        raise PatternRegistryError("registry ref could not be read: 503")


# --- nonterminal outcomes require a live case file (#7248 round 6 F8/A3) ----


class _FreshIssue:
    """A fresh reader over one issue's real labels and state."""

    def __init__(self, state: str = "open", *, fails: bool = False) -> None:
        self.state = state
        self.fails = fails
        self.reads: list[int] = []

    def read_issue_labels(self, issue_number: int) -> list[str]:
        self.reads.append(issue_number)
        if self.fails:
            raise FreshIssueReadError("transport failed")
        return []

    def read_issue_snapshot(self, issue_number: int) -> FreshIssueSnapshot:
        self.reads.append(issue_number)
        if self.fails:
            raise FreshIssueReadError("transport failed")
        return FreshIssueSnapshot(number=issue_number, labels=(), state=self.state)


def _gated(reader: _FreshIssue) -> GatedCaseFileMutationAuthority:
    return GatedCaseFileMutationAuthority(
        gate=ReconciliationGate(
            fresh_issue_reader=cast(Any, reader),
            reconcile=True,
            fresh_issue_snapshot_reader=cast(Any, reader),
        ),
        pause_label="io:needs-reconcile",
    )


def test_a_closed_case_file_refuses_a_nonterminal_classification() -> None:
    """``active`` means the case file stays OPEN, so it must still be open.

    The guard used to check only that the pause label was absent, and a closed
    issue has perfectly readable labels — so a human closing a case file after
    the plan was reviewed sailed through it, and durable authority was left
    saying ``active`` about an issue the board shows closed (#7248 round 6
    review F8).
    """
    reader = _FreshIssue(state="closed")
    reconciler, registry, repository = _reconciler(
        ("a", 1), mutation_authority=_gated(reader)
    )
    plan = _plan(_reviewed_outcome(registry, "a", 1, "active"))

    result = reconciler.run(plan, configured_repository="owner/repo", apply_writes=True)

    assert result.applied == 0
    assert result.failures and "issue state mismatch" in result.failures[0]
    assert registry.read(signature="a").lifecycle == ()  # type: ignore[union-attr]
    assert repository.comments == [] and repository.closed == []


def test_an_unreadable_case_file_refuses_a_nonterminal_classification() -> None:
    """Unknown is not open. A failed read fails closed, like every other one."""
    reader = _FreshIssue(fails=True)
    reconciler, registry, repository = _reconciler(
        ("a", 1), mutation_authority=_gated(reader)
    )
    plan = _plan(_reviewed_outcome(registry, "a", 1, "needs_human"))

    result = reconciler.run(plan, configured_repository="owner/repo", apply_writes=True)

    assert result.applied == 0
    assert result.failures
    assert registry.read(signature="a").lifecycle == ()  # type: ignore[union-attr]
    assert repository.comments == [] and repository.closed == []


def test_an_open_case_file_accepts_a_nonterminal_classification() -> None:
    """The guard is a guard, not a block: an open case file still records."""
    reader = _FreshIssue(state="open")
    reconciler, registry, repository = _reconciler(
        ("a", 1), mutation_authority=_gated(reader)
    )
    plan = _plan(_reviewed_outcome(registry, "a", 1, "needs_human"))

    result = reconciler.run(plan, configured_repository="owner/repo", apply_writes=True)

    assert result.ok and result.applied == 1
    assert registry.read(signature="a").disposition == "needs_human"  # type: ignore[union-attr]
    assert reader.reads == [1]
    assert repository.comments == [] and repository.closed == []


def test_a_gate_with_no_snapshot_reader_refuses_rather_than_narrowing() -> None:
    """An expectation the gate cannot verify is unknown, not satisfied.

    This is the composition failure mode the wiring must never reach: a gate
    holding only a labels reader can still answer the pause-label half of a
    classification expectation. Answering it would be worse than useless, so
    the gate refuses the whole expectation instead (#7248 round 6 review A3).
    """
    reader = _FreshIssue(state="open")
    authority = GatedCaseFileMutationAuthority(
        gate=ReconciliationGate(
            fresh_issue_reader=cast(Any, reader), reconcile=True
        ),
        pause_label="io:needs-reconcile",
    )

    authority.require_retirement(1)  # labels-only expectation: still verifiable

    with pytest.raises(ReconciliationRequired):
        authority.require_classification(1)


def test_a_terminal_outcome_does_not_require_the_case_file_to_be_open() -> None:
    """Retirement closes the issue; an already-closed one is idempotent.

    The stricter grant belongs to classifications alone. Requiring ``open`` for
    a retirement would make the command unable to finish its own interrupted
    work — the close lands, the registry commit does not, and the retry finds a
    closed issue.
    """
    reader = _FreshIssue(state="closed")
    reconciler, registry, repository = _reconciler(
        ("a", 1), mutation_authority=_gated(reader)
    )
    plan = _plan(_reviewed_outcome(registry, "a", 1, "shipped"))

    result = reconciler.run(plan, configured_repository="owner/repo", apply_writes=True)

    assert result.ok and result.applied == 1
    assert repository.closed == [1]
    assert registry.read(signature="a").disposition == "shipped"  # type: ignore[union-attr]


def test_each_outcome_asks_for_the_authority_its_own_write_needs() -> None:
    """The kind of grant follows the kind of write, per outcome."""
    authority = _Authority()
    reconciler, registry, _repository = _reconciler(
        ("a", 1), ("b", 2), mutation_authority=authority
    )
    plan = _plan(
        _reviewed_outcome(registry, "a", 1, "needs_human"),
        _reviewed_outcome(registry, "b", 2, "superseded"),
    )

    reconciler.run(plan, configured_repository="owner/repo", apply_writes=True)

    assert ("classification", 1) in authority.calls
    assert ("retirement", 2) in authority.calls
    assert not any(kind == "classification" for kind, issue in authority.calls if issue == 2)
