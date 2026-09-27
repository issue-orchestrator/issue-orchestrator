"""Durable, cross-client pattern case-file lifecycle tests (#7240)."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest

from issue_orchestrator.adapters.github.pattern_registry import GitHubRefPatternRegistry
from issue_orchestrator.control.tech_lead_case_file_lifecycle import (
    PatternCaseFileLifecycleOwner,
    retirement_comment,
)
from issue_orchestrator.control.reconciliation import (
    ExternalSnapshot,
    ReconciliationRequired,
)
from issue_orchestrator.control.tech_lead_case_file_owner import (
    AmbiguousPatternPublicationError,
)
from issue_orchestrator.domain.tech_lead_findings import (
    CASE_FILE_ACTIVE,
    CASE_FILE_DECLINED,
    CASE_FILE_NEEDS_HUMAN,
    CaseFileClassification,
    CaseFileLifecycleTransition,
    PatternObservation,
    PendingCaseFile,
)
from issue_orchestrator.ports.comment_receipt import IssueCommentReceipt
from issue_orchestrator.ports.pattern_registry import (
    PatternRegistryError,
    PatternReservationState,
    TerminalRetirementPolicy,
)

from tests.unit.adapters.github.test_ref_claim_adapter import FakeGitHubRefClient


REFUSE = TerminalRetirementPolicy.REFUSE
ADOPT = TerminalRetirementPolicy.ADOPT

CASE_FILE = 81
"""The canonical case file ``_seed`` binds to the ``stale-pattern`` signature."""


def _registry(
    client: FakeGitHubRefClient, claimant: str, now: list[datetime]
) -> GitHubRefPatternRegistry:
    return GitHubRefPatternRegistry(
        cast(Any, client),
        claimant_id=claimant,
        lease_seconds=30,
        clock=lambda: now[0],
    )


def _seed(registry: GitHubRefPatternRegistry) -> None:
    pending = PendingCaseFile(
        signature="stale-pattern",
        title="Pattern case file: stale-pattern",
        idempotency_marker="<!-- stale-pattern -->",
        body_observation_id="run:session:A1",
    )
    reserved = registry.reserve(pending)
    registry.finalize(
        signature=pending.signature,
        reservation_id=reserved.entry.reservation_id,
        issue_number=CASE_FILE,
    )


def _transition(
    disposition: str = CASE_FILE_DECLINED,
) -> CaseFileLifecycleTransition:
    return CaseFileLifecycleTransition(
        transition_id="plan-2026-09-10:stale-pattern",
        disposition=disposition,  # type: ignore[arg-type]
        reason="The reviewed promotion was deliberately declined.",
        evidence=("promotion owner/repo#199 closed without a merged PR",),
        recorded_at="2026-09-10T12:00:00+00:00",
    )


class _Repository:
    def __init__(self) -> None:
        self.comments: list[tuple[int, str]] = []
        self.closures: list[int] = []
        self.fail_comment = False

    def find_issue_comment_receipt(
        self, issue_number: int, *, body: str
    ) -> IssueCommentReceipt | None:
        if (issue_number, body) not in self.comments:
            return None
        return IssueCommentReceipt(
            comment_id="1",
            url="https://example.test/comments/1",
            author_key="app:test",
            body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        )

    def add_comment(self, issue_number: int, body: str) -> str:
        if self.fail_comment:
            raise RuntimeError("ambiguous transport failure")
        self.comments.append((issue_number, body))
        return "https://example.test/comments/1"

    def update_issue_state(self, issue_number: int, state: str) -> None:
        assert state == "closed"
        self.closures.append(issue_number)


def _owner(
    registry: GitHubRefPatternRegistry,
    repository: _Repository,
    *,
    before_write: Any = None,
) -> PatternCaseFileLifecycleOwner:
    if before_write is None:
        return PatternCaseFileLifecycleOwner(
            registry=registry, repository_host=cast(Any, repository)
        )
    return PatternCaseFileLifecycleOwner(
        registry=registry,
        repository_host=cast(Any, repository),
        before_write=before_write,
    )


def test_two_clients_converge_on_one_terminal_disposition() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    first = _registry(client, "engine-a", now)
    second = _registry(client, "engine-b", now)
    _seed(first)
    repository = _Repository()

    outcome = _owner(first, repository).retire(
        signature="stale-pattern", transition=_transition(), issue_number=CASE_FILE,
        already_terminal=REFUSE,
    )
    replay = _owner(second, repository).retire(
        signature="stale-pattern", transition=_transition(), issue_number=CASE_FILE,
        already_terminal=REFUSE,
    )

    assert not outcome.deduplicated
    assert replay.deduplicated
    assert len(repository.comments) == 1
    assert repository.closures == [81]
    entry = second.read(signature="stale-pattern")
    assert entry is not None
    assert entry.disposition == CASE_FILE_DECLINED
    assert entry.lifecycle == (_transition(),)


def test_ambiguous_comment_is_never_reissued_and_later_receipt_recovers() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    first = _registry(client, "engine-a", now)
    second = _registry(client, "engine-b", now)
    _seed(first)
    repository = _Repository()
    repository.fail_comment = True

    with pytest.raises(RuntimeError, match="ambiguous transport"):
        _owner(first, repository).retire(
            signature="stale-pattern", transition=_transition(), issue_number=CASE_FILE,
            already_terminal=REFUSE,
        )
    now[0] += timedelta(hours=1)
    with pytest.raises(AmbiguousPatternPublicationError):
        _owner(second, repository).retire(
            signature="stale-pattern", transition=_transition(), issue_number=CASE_FILE,
            already_terminal=REFUSE,
        )

    repository.fail_comment = False
    repository.comments.append((81, retirement_comment(_transition())))
    recovered = _owner(second, repository).retire(
        signature="stale-pattern", transition=_transition(), issue_number=CASE_FILE,
        already_terminal=REFUSE,
    )

    assert not recovered.deduplicated
    assert len(repository.comments) == 1
    assert repository.closures == [81]


def test_restart_after_close_phase_finishes_without_reposting_comment() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)
    _seed(registry)
    transition = _transition()
    comment = retirement_comment(transition)
    reserved = registry.reserve_retirement(
        signature="stale-pattern",
        transition=transition,
        comment=comment,
        issue_number=CASE_FILE,
    )
    registry.begin_retirement_publication(
        signature="stale-pattern", reservation_id=reserved.entry.reservation_id
    )
    registry.confirm_retirement_comment(
        signature="stale-pattern", reservation_id=reserved.entry.reservation_id
    )
    repository = _Repository()
    repository.comments.append((81, comment))
    repository.closures.append(81)  # the first process closed, then crashed

    _owner(registry, repository).retire(
        signature="stale-pattern", transition=transition, issue_number=CASE_FILE,
        already_terminal=REFUSE,
    )

    assert len(repository.comments) == 1
    assert repository.closures == [81, 81]
    entry = registry.read(signature="stale-pattern")
    assert entry is not None and entry.disposition == CASE_FILE_DECLINED


def test_retry_keeps_first_recorded_timestamp_authoritative() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)
    _seed(registry)
    repository = _Repository()
    original = _transition()
    first = _owner(registry, repository).retire(
        signature="stale-pattern", transition=original, issue_number=CASE_FILE,
        already_terminal=REFUSE,
    )
    later_retry = CaseFileLifecycleTransition(
        transition_id=original.transition_id,
        disposition=original.disposition,
        reason=original.reason,
        evidence=original.evidence,
        recorded_at="2026-09-11T12:00:00+00:00",
    )

    replay = _owner(registry, repository).retire(
        signature="stale-pattern", transition=later_retry, issue_number=CASE_FILE,
        already_terminal=REFUSE,
    )

    assert replay.deduplicated
    entry = registry.read(signature="stale-pattern")
    assert entry is not None and entry.lifecycle == (original,)
    # The OUTCOME must agree with durable authority, not with the attempt that
    # produced it: a retry that reported its own later clock would hand the next
    # layer a timestamp the registry contradicts (#7247 review F1).
    assert first.transition == original
    assert replay.transition == original
    assert replay.transition.recorded_at == "2026-09-10T12:00:00+00:00"


def test_retirement_refuses_a_case_file_the_command_did_not_authorize() -> None:
    """A guarded command must never close an issue its gate did not check.

    The command is authorized against ``case_file_issue_number``; the registry
    names the canonical case file independently. If those drift, the gate would
    approve issue A while the owner closes issue B (#7247 review F2).
    """
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)
    _seed(registry)
    repository = _Repository()

    with pytest.raises(PatternRegistryError, match="refusing to mutate"):
        _owner(registry, repository).retire(
            signature="stale-pattern",
            transition=_transition(),
            issue_number=CASE_FILE + 1,
            already_terminal=REFUSE,
        )

    assert repository.comments == []
    assert repository.closures == []
    entry = registry.read(signature="stale-pattern")
    assert entry is not None
    assert entry.issue_number == CASE_FILE
    assert entry.lifecycle == ()
    assert entry.pending_retirement is None
    assert entry.disposition == CASE_FILE_ACTIVE


def test_terminal_signature_accepts_later_evidence_without_reopening() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)
    _seed(registry)
    repository = _Repository()
    _owner(registry, repository).retire(
        signature="stale-pattern", transition=_transition(), issue_number=CASE_FILE,
        already_terminal=REFUSE,
    )

    admitted = registry.reserve_observation(
        signature="stale-pattern",
        observation=PatternObservation(
            observation_id="run:session:A2", comment="It happened again"
        ),
        classification=CaseFileClassification(),
        issue_number=CASE_FILE,
    )
    assert admitted.state is PatternReservationState.ACQUIRED
    registry.finalize_observation(
        signature="stale-pattern", reservation_id=admitted.entry.reservation_id
    )

    entry = registry.read(signature="stale-pattern")
    assert entry is not None
    assert entry.disposition == CASE_FILE_DECLINED
    assert entry.observation_ids[-1] == "run:session:A2"


@pytest.mark.parametrize("disposition", (CASE_FILE_ACTIVE, CASE_FILE_NEEDS_HUMAN))
def test_nonterminal_reconciliation_records_auditable_outcome(
    disposition: str,
) -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)
    _seed(registry)
    transition = _transition(disposition)

    first = _owner(registry, _Repository()).classify(
        signature="stale-pattern", transition=transition
    )
    replay = _owner(registry, _Repository()).classify(
        signature="stale-pattern", transition=transition
    )

    assert first == replay
    assert first.disposition == disposition
    assert first.lifecycle == (transition,)


#: The retirement's guard call that immediately precedes ``add_comment``.
#: Index 0 is the guard ``retire`` runs before reserving, which refuses before
#: anything is reserved and so proves nothing about publication state.
PUBLICATION_GUARD_CALL = 1


class _RefusingGuard:
    """A mutation guard that declines ONE nominated call (#7248 F6).

    The real refusals are transient and ordinary: a ``needs-reconcile`` pause
    label appears between ticks, or the fresh label read fails. Neither attempts
    a write. Refusing a nominated call index rather than "the first N" is what
    puts the refusal exactly between publication-state admission and
    ``add_comment`` -- the only window in which the bug is observable.
    """

    def __init__(self, refuse_call: int) -> None:
        self.refuse_call = refuse_call
        self.calls = 0

    def __call__(self) -> None:
        index = self.calls
        self.calls += 1
        if index == self.refuse_call:
            raise ReconciliationRequired(
                "issue",
                CASE_FILE,
                ExternalSnapshot(number=CASE_FILE, labels=frozenset()),
                ExternalSnapshot(
                    number=CASE_FILE, labels=frozenset({"io:needs-reconcile"})
                ),
                reason="a pause label appeared mid-retirement",
            )


def test_a_guard_refusal_before_any_write_leaves_the_retirement_resumable() -> None:
    """An authorized-but-unattempted publication must stay resumable (#7248 F6).

    The owner used to record ``publication_started_at`` BEFORE the receipt
    pre-check and before the mutation guard ran. When the guard then declined,
    nothing had been posted, yet the durable state said "publication in flight"
    -- and every later attempt demanded a receipt that could not exist, so the
    retirement was permanently stuck.
    """
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)
    _seed(registry)
    repository = _Repository()
    guard = _RefusingGuard(refuse_call=PUBLICATION_GUARD_CALL)

    with pytest.raises(ReconciliationRequired):
        _owner(registry, repository, before_write=guard).retire(
            signature="stale-pattern", transition=_transition(), issue_number=CASE_FILE,
            already_terminal=REFUSE,
        )

    # Nothing was posted, so nothing may be recorded as in flight.
    assert repository.comments == []
    entry = registry.read(signature="stale-pattern")
    assert entry is not None
    assert entry.publication_started_at is None

    # Authority returns; the exact same retirement replays to completion.
    recovered = _owner(registry, repository).retire(
        signature="stale-pattern", transition=_transition(), issue_number=CASE_FILE,
        already_terminal=REFUSE,
    )

    assert not recovered.deduplicated
    assert len(repository.comments) == 1
    assert repository.closures == [CASE_FILE]


def test_a_guard_refusal_does_not_mask_a_genuinely_ambiguous_write() -> None:
    """The other half of the distinction must still hold (#7248 F6).

    Moving the marker must not weaken the ambiguous case: once ``add_comment``
    has actually been attempted, a retry still requires a receipt rather than
    posting a second comment.
    """
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)
    _seed(registry)
    repository = _Repository()
    repository.fail_comment = True

    with pytest.raises(RuntimeError, match="ambiguous transport"):
        _owner(registry, repository).retire(
            signature="stale-pattern", transition=_transition(), issue_number=CASE_FILE,
            already_terminal=REFUSE,
        )

    entry = registry.read(signature="stale-pattern")
    assert entry is not None
    assert entry.publication_started_at is not None
    now[0] += timedelta(hours=1)
    with pytest.raises(AmbiguousPatternPublicationError):
        _owner(registry, repository).retire(
            signature="stale-pattern", transition=_transition(), issue_number=CASE_FILE,
            already_terminal=REFUSE,
        )
    assert repository.comments == []


def test_a_concurrent_seed_cannot_move_a_reserved_retirements_revision() -> None:
    """Seeding must not change an entry a reviewed decision is riding on (#7248 F2).

    The interleaving: a plan reserves a retirement against an exact reviewed
    revision; another client then deploys/upgrades and seeds its local rows,
    merging extra observation ids and classification into the SAME entry; the
    first client retries. Retirement recovery accepts the existing pending
    retirement before it ever reaches the revision check, so the stale reviewed
    decision would comment on and close the issue against durable evidence that
    had changed underneath it.
    """
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)
    _seed(registry)
    repository = _Repository()
    repository.fail_comment = True

    reviewed = registry.read(signature="stale-pattern")
    assert reviewed is not None
    reviewed_evidence = (reviewed.observation_ids, reviewed.classification)

    # The plan reserves, and its publication is left ambiguous.
    with pytest.raises(RuntimeError, match="ambiguous transport"):
        _owner(registry, repository).retire(
            signature="stale-pattern", transition=_transition(), issue_number=CASE_FILE,
            already_terminal=REFUSE,
        )
    reserved = registry.read(signature="stale-pattern")
    assert reserved is not None
    assert reserved.pending_retirement is not None

    # A second client deploys and seeds richer local evidence for the SAME
    # signature while that retirement is in flight.
    other = _registry(client, "engine-b", now)
    other.seed_committed(
        (
            replace(
                reserved,
                pending_retirement=None,
                publication_started_at=None,
                observation_ids=(*reserved.observation_ids, "run:session:B7"),
            ),
        )
    )

    # ``review_revision()`` refuses to answer while an effect is in flight, so
    # the invariant is asserted on the durable fields it is computed from.
    after_seed = registry.read(signature="stale-pattern")
    assert after_seed is not None
    assert (after_seed.observation_ids, after_seed.classification) == reviewed_evidence, (
        "the seed moved the evidence the reserved decision was reviewed against"
    )
    assert after_seed.pending_retirement == reserved.pending_retirement

    # And once the retirement settles, the deferred evidence is still merged.
    repository.fail_comment = False
    repository.comments.append((CASE_FILE, retirement_comment(_transition())))
    _owner(registry, repository).retire(
        signature="stale-pattern", transition=_transition(), issue_number=CASE_FILE,
        already_terminal=REFUSE,
    )
    settled = registry.read(signature="stale-pattern")
    assert settled is not None
    other.seed_committed(
        (
            replace(
                settled,
                observation_ids=(*settled.observation_ids, "run:session:B7"),
            ),
        )
    )
    merged = registry.read(signature="stale-pattern")
    assert merged is not None
    assert "run:session:B7" in merged.observation_ids


def test_a_seed_still_merges_once_no_effect_is_in_flight() -> None:
    """Deferral is not discard: the evidence lands after the effect settles."""
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)
    _seed(registry)
    settled = registry.read(signature="stale-pattern")
    assert settled is not None

    registry.seed_committed(
        (replace(settled, observation_ids=(*settled.observation_ids, "run:session:B7")),)
    )

    merged = registry.read(signature="stale-pattern")
    assert merged is not None
    assert "run:session:B7" in merged.observation_ids


# --- a signature already retired under ANOTHER transition id (#7345) ---------


def _other_writer_retired(disposition: str = "shipped") -> CaseFileLifecycleTransition:
    return CaseFileLifecycleTransition(
        transition_id="promotion:stale-pattern:owner/repo:199:declined",
        disposition=disposition,  # type: ignore[arg-type]
        reason="Promoted issue owner/repo#199 was closed without a merged fix.",
        evidence=("owner/repo#199",),
        recorded_at="2026-09-11T12:00:00+00:00",
    )


def _local_registry() -> Any:
    from issue_orchestrator.control.pattern_registry import (
        LocalPatternCaseFileRegistry,
    )
    from issue_orchestrator.ports.tech_lead_authority import (
        InMemoryTechLeadAuthorityStore,
    )

    authority = InMemoryTechLeadAuthorityStore()
    authority.record_pattern(
        signature="stale-pattern",
        issue_number=CASE_FILE,
        observation_id="run:session:A1",
    )
    return LocalPatternCaseFileRegistry(authority)


def _shared_registry() -> Any:
    registry = _registry(
        FakeGitHubRefClient(), "engine-a", [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    )
    _seed(registry)
    return registry


REGISTRIES = pytest.mark.parametrize(
    "make_registry", (_shared_registry, _local_registry), ids=("shared", "local")
)


@REGISTRIES
def test_an_observed_outcome_adopts_the_recorded_terminal_transition(
    make_registry: Any,
) -> None:
    """A different disposition under a different id is ADOPTED, not refused."""
    registry = make_registry()
    first = _Repository()
    _owner(registry, first).retire(
        signature="stale-pattern",
        transition=_other_writer_retired(),
        issue_number=CASE_FILE,
        already_terminal=REFUSE,
    )
    repository = _Repository()

    outcome = _owner(registry, repository).retire(
        signature="stale-pattern",
        transition=_transition(CASE_FILE_DECLINED),
        issue_number=CASE_FILE,
        already_terminal=ADOPT,
    )

    assert outcome.adopted and outcome.deduplicated
    assert outcome.transition == _other_writer_retired()
    assert repository.comments == [] and repository.closures == []
    entry = registry.read(signature="stale-pattern")
    assert entry is not None
    assert [item.transition_id for item in entry.lifecycle] == [
        _other_writer_retired().transition_id
    ]


@REGISTRIES
def test_a_reviewed_decision_still_refuses_a_signature_retired_under_another_id(
    make_registry: Any,
) -> None:
    """REFUSE stays fail-fast: changing a terminal disposition is a reopen."""
    registry = make_registry()
    _owner(registry, _Repository()).retire(
        signature="stale-pattern",
        transition=_other_writer_retired(),
        issue_number=CASE_FILE,
        already_terminal=REFUSE,
    )
    repository = _Repository()

    with pytest.raises(PatternRegistryError, match="is terminal"):
        _owner(registry, repository).retire(
            signature="stale-pattern",
            transition=_transition(CASE_FILE_DECLINED),
            issue_number=CASE_FILE,
            already_terminal=REFUSE,
        )

    assert repository.comments == [] and repository.closures == []


@REGISTRIES
def test_adoption_never_reopens_a_terminal_signature(make_registry: Any) -> None:
    """Only a TERMINAL request is answered by adoption; a reopen stays refused."""
    from issue_orchestrator.ports.pattern_registry import (
        admit_lifecycle_transition,
        resolve_admitted_transition,
    )

    registry = make_registry()
    _owner(registry, _Repository()).retire(
        signature="stale-pattern",
        transition=_other_writer_retired(),
        issue_number=CASE_FILE,
        already_terminal=REFUSE,
    )
    entry = registry.read(signature="stale-pattern")
    assert entry is not None
    reopen = _transition(CASE_FILE_ACTIVE)

    assert resolve_admitted_transition(entry, reopen, already_terminal=ADOPT) is None
    with pytest.raises(PatternRegistryError, match="is terminal"):
        admit_lifecycle_transition(entry, reopen, already_terminal=ADOPT)
    with pytest.raises(PatternRegistryError, match="is terminal"):
        _owner(registry, _Repository()).classify(
            signature="stale-pattern", transition=reopen
        )


@REGISTRIES
def test_adoption_does_not_mask_a_reused_id_with_a_changed_payload(
    make_registry: Any,
) -> None:
    """Identity conflicts are still refused before adoption is considered."""
    registry = make_registry()
    _owner(registry, _Repository()).retire(
        signature="stale-pattern",
        transition=_other_writer_retired(),
        issue_number=CASE_FILE,
        already_terminal=REFUSE,
    )
    changed = replace(_other_writer_retired(), reason="A different reviewed intent.")

    with pytest.raises(PatternRegistryError, match="changed payload"):
        _owner(registry, _Repository()).retire(
            signature="stale-pattern",
            transition=changed,
            issue_number=CASE_FILE,
            already_terminal=ADOPT,
        )


class _CrashOnClose(_Repository):
    def update_issue_state(self, issue_number: int, state: str) -> None:
        raise RuntimeError("the other writer's process died before closing")


@REGISTRIES
def test_an_observed_outcome_resumes_another_writers_admitted_retirement(
    make_registry: Any,
) -> None:
    """ADOPT covers a terminal intent that is admitted but not yet committed.

    The other writer confirmed its comment and stopped before the close. Its
    reservation is durable, so an exact-intent resume rule refused the
    settlement forever. Adoption finishes THAT transition, with ITS comment,
    and reports it."""
    registry = make_registry()
    with pytest.raises(RuntimeError):
        _owner(registry, _CrashOnClose()).retire(
            signature="stale-pattern",
            transition=_other_writer_retired(),
            issue_number=CASE_FILE,
            already_terminal=REFUSE,
        )
    repository = _Repository()

    outcome = _owner(registry, repository).retire(
        signature="stale-pattern",
        transition=_transition(CASE_FILE_DECLINED),
        issue_number=CASE_FILE,
        already_terminal=ADOPT,
    )

    assert outcome.adopted
    assert outcome.transition == _other_writer_retired()
    assert repository.comments == []
    assert repository.closures == [CASE_FILE]
    entry = registry.read(signature="stale-pattern")
    assert entry is not None and entry.pending_retirement is None
    assert [item.transition_id for item in entry.lifecycle] == [
        _other_writer_retired().transition_id
    ]


@REGISTRIES
def test_a_reviewed_decision_still_refuses_another_writers_pending_retirement(
    make_registry: Any,
) -> None:
    registry = make_registry()
    with pytest.raises(RuntimeError):
        _owner(registry, _CrashOnClose()).retire(
            signature="stale-pattern",
            transition=_other_writer_retired(),
            issue_number=CASE_FILE,
            already_terminal=REFUSE,
        )

    with pytest.raises(PatternRegistryError, match="different retirement in flight"):
        _owner(registry, _Repository()).retire(
            signature="stale-pattern",
            transition=_transition(CASE_FILE_DECLINED),
            issue_number=CASE_FILE,
            already_terminal=REFUSE,
        )
