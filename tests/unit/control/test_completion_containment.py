"""The completion pass confines one session's failure to that session (#8000, #8693)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_orchestrator.control.completion_containment import CompletionContainment
from issue_orchestrator.control.completion_dispatcher import CompletedDecision
from issue_orchestrator.control.issue_fetch_resilience import PermanentIssueFetchError
from issue_orchestrator.domain.action_liveness import Admission, LivenessPolicy, OutcomeKind
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import AgentConfig, Issue, Session
from issue_orchestrator.domain.session_key import SessionKey
from issue_orchestrator.domain.session_kind import SessionKind
from tests.unit.control.liveness_doubles import ManualClock, RecordingEscalation, liveness_owner
from tests.unit.session_run_helpers import make_session_run_assets


def _session(tmp_path: Path, number: int, run: str = "20260603T000000000000Z") -> Session:
    worktree = tmp_path / f"wt-{number}-{run}"
    return Session(
        key=SessionKey(issue=FakeIssueKey(str(number)), kind=SessionKind.REWORK),
        issue=Issue(number=number, title="t", labels=[]),
        agent_config=AgentConfig(prompt_path=Path("/tmp/p.txt"), model="sonnet", timeout_minutes=5),
        terminal_id=f"rework-{number}",
        worktree_path=worktree,
        branch_name=f"{number}-b",
        run_assets=make_session_run_assets(worktree, session_name=f"rework-{number}", run_id=run),
    )


def _containment(policy: LivenessPolicy = LivenessPolicy()):
    escalation = RecordingEscalation()
    return CompletionContainment(liveness_owner(escalation=escalation, policy=policy)), escalation


def _active(_session: Session) -> bool:
    return True


def _dropped(_session: Session) -> bool:
    return False


def _row(containment: CompletionContainment, session: Session):
    return containment.owner.admit(CompletionContainment.key(session)).row


def test_a_failed_apply_parks_its_session_and_escalates_its_issue(tmp_path):
    containment, escalation = _containment()
    failing, sibling = _session(tmp_path, 450), _session(tmp_path, 451)
    applied: list[str] = []

    def apply(completed: CompletedDecision) -> None:
        applied.append(completed.session.terminal_id)
        if completed.session is failing:
            raise RuntimeError("Can't trigger event needs_human from state pr_pending!")

    containment.apply_each(
        [CompletedDecision(failing, None, None), CompletedDecision(sibling, None, None)],
        apply,
        in_pass=_dropped,
    )

    assert applied == ["rework-450", "rework-451"]
    row = _row(containment, failing)
    assert row is not None and row.parked and row.last_outcome is OutcomeKind.PERMANENT
    assert not containment.admit(failing)
    assert [r.key.escalation_issue for r in escalation.committed_blocks] == [450]
    assert _row(containment, sibling) is None


def test_a_failed_decision_backs_off_and_parks_once_its_budget_is_spent(tmp_path):
    """The session is still active, so the next tick retries it: a transient row."""
    containment, escalation = _containment(LivenessPolicy(max_attempts=2))
    session = _session(tmp_path, 450)

    def apply(completed: CompletedDecision) -> None:
        assert completed.error is not None
        raise completed.error

    decided = CompletedDecision(session, None, RuntimeError("decide failed"))
    containment.apply_each([decided], apply, in_pass=_active)
    row = _row(containment, session)
    assert row is not None and not row.parked and row.last_outcome is OutcomeKind.TRANSIENT
    assert containment.owner.admit(CompletionContainment.key(session)).admission is Admission.BACKING_OFF
    assert escalation.blocks == []

    containment.apply_each([decided], apply, in_pass=_active)
    assert _row(containment, session).parked
    assert [r.key.escalation_issue for r in escalation.committed_blocks] == [450]


def test_a_failed_observation_is_confined_and_retried(tmp_path):
    containment, _ = _containment()
    session = _session(tmp_path, 450)

    def observe(_session: Session):
        raise FileNotFoundError("run dir missing")

    assert containment.observe(session, observe) is None
    row = _row(containment, session)
    assert row is not None and "run dir missing" in row.last_reason and not row.parked


def test_a_later_success_settles_the_recorded_failure(tmp_path):
    containment, _ = _containment()
    session = _session(tmp_path, 450)
    decided = CompletedDecision(session, None, RuntimeError("decide failed"))
    containment.apply_each(
        [decided], lambda completed: (_ for _ in ()).throw(completed.error), in_pass=_active
    )
    assert _row(containment, session) is not None

    containment.apply_each(
        [CompletedDecision(session, None, None)], lambda _completed: None, in_pass=_dropped
    )

    assert _row(containment, session) is None


def test_a_new_run_of_the_same_terminal_is_not_held_by_an_old_park(tmp_path):
    containment, _ = _containment()
    old = _session(tmp_path, 450, run="20261007T205402000000Z")
    new = _session(tmp_path, 450, run="20261008T010000000000Z")
    assert old.run_assets.identity != new.run_assets.identity

    def apply(_completed: CompletedDecision) -> None:
        raise RuntimeError("boom")

    containment.apply_each([CompletedDecision(old, None, None)], apply, in_pass=_dropped)

    assert not containment.admit(old)
    assert containment.admit(new)


@pytest.mark.parametrize(
    "error",
    [
        PermanentIssueFetchError(SimpleNamespace(summary="auth revoked", suggested_fix="re-auth")),
        KeyboardInterrupt(),
    ],
    ids=["permanent-fetch", "interrupt"],
)
def test_an_engine_wide_fault_still_reaches_the_loop_after_every_sibling(tmp_path, error):
    containment, _ = _containment()
    failing, sibling = _session(tmp_path, 450), _session(tmp_path, 451)
    applied: list[str] = []

    def apply(completed: CompletedDecision) -> None:
        applied.append(completed.session.terminal_id)
        if completed.session is failing:
            raise error

    with pytest.raises(type(error)):
        containment.apply_each(
            [CompletedDecision(failing, None, None), CompletedDecision(sibling, None, None)],
            apply,
            in_pass=_active,
        )

    assert applied == ["rework-450", "rework-451"]
    assert _row(containment, failing) is None


def test_an_apply_that_fails_before_the_session_leaves_the_pass_is_retried(tmp_path):
    """r3 F1: retryability is whether the session is still in the pass, not which step raised."""
    clock = ManualClock()
    escalation = RecordingEscalation()
    containment = CompletionContainment(liveness_owner(escalation=escalation, clock=clock))
    session = _session(tmp_path, 450)
    failures = [OSError("claim store unavailable")]

    def apply(_completed: CompletedDecision) -> None:
        if failures:
            raise failures.pop()

    decided = CompletedDecision(session, None, None)
    containment.apply_each([decided], apply, in_pass=_active)
    row = _row(containment, session)
    assert row is not None and row.last_outcome is OutcomeKind.TRANSIENT and not row.parked
    assert not containment.admit(session)

    clock.advance(LivenessPolicy().max_backoff)
    assert containment.admit(session)
    containment.apply_each([decided], apply, in_pass=_dropped)

    assert _row(containment, session) is None
    assert escalation.blocks == []
