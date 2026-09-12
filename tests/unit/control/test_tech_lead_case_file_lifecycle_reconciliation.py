"""Reviewed backlog lifecycle reconciliation tests (#7240)."""

from __future__ import annotations

import hashlib
from typing import Any, cast

import pytest

from issue_orchestrator.control.pattern_registry import LocalPatternCaseFileRegistry
from issue_orchestrator.control.mutation_gate import ReconciliationGate
from issue_orchestrator.control.reconciliation import ExpectedState
from issue_orchestrator.control.tech_lead_case_file_lifecycle_reconciliation import (
    CaseFileLifecycleReconciler,
    CaseFileLifecycleReconciliationPlan,
    CaseFileLifecycleReconciliationRefused,
)
from issue_orchestrator.domain.tech_lead_findings import CaseFileLifecycleTransition
from issue_orchestrator.ports.comment_receipt import IssueCommentReceipt
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


def _reconciler(
    *rows: tuple[str, int],
    require_mutation_authority=lambda _issue: None,
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
            require_mutation_authority=require_mutation_authority,
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
        ("a", 1), require_mutation_authority=race
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
    expected = ExpectedState.with_labels(forbidden={"io:needs-reconcile"})
    reconciler, registry, repository = _reconciler(
        ("a", 1),
        require_mutation_authority=lambda issue: gate.require_state(expected, issue),
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
    guarded: list[int] = []
    reconciler, registry, repository = _reconciler(
        ("a", 1), ("b", 2), require_mutation_authority=guarded.append
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

    assert guarded == []
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
    guarded: list[int] = []
    reconciler, registry, repository = _reconciler(
        ("a", 1), ("b", 2), require_mutation_authority=guarded.append
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

    assert guarded == []
    assert repository.comments == []
    assert repository.closed == []
    assert registry.read(signature="a").lifecycle == ()  # type: ignore[union-attr]


def test_preflight_resumes_an_interrupted_retirement_of_the_same_intent() -> None:
    """An in-flight terminal write of this plan's own intent is a resume.

    Its entry has no review revision to compare — a pending external effect
    means the settled facts are not yet knowable — so preflight must recognise
    the resume before it reaches the staleness rule, exactly as the registries
    do inside their reserving compare-and-swap.
    """
    reconciler, registry, repository = _reconciler(("a", 1))
    plan = _plan(_reviewed_outcome(registry, "a", 1, "shipped"))
    registry.reserve_retirement(
        signature="a",
        transition=plan.outcomes[0].transition(
            plan_id=plan.plan_id, recorded_at=plan.recorded_at
        ),
        comment="<!-- retirement -->",
        issue_number=1,
    )

    result = reconciler.run(
        plan, configured_repository="owner/repo", apply_writes=False
    )

    assert result.dry_run and result.terminal == 1
    assert repository.comments == [] and repository.closed == []


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
        require_mutation_authority=lambda _issue: None,
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
