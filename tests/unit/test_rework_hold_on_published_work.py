"""A rework's hold on a PR_PENDING issue, end to end through one engine tick (#8693, #8000).

On porchpin (2026-10-07 20:56:57, iteration 187; earlier 2026-08-08 iterations
610 and 630) a rework session on an issue whose PR was pending completed with
``needs_human``. The completion handler drove the issue's cached lifecycle as
if the issue were IN_PROGRESS, the issue machine raised ``Can't trigger event
needs_human from state pr_pending!``, and the exception aborted the WHOLE
iteration: every other session's completion and all planning were skipped.
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from issue_orchestrator.control.completion_containment import CompletionContainment
from issue_orchestrator.control.session_controller import SessionDecision
from issue_orchestrator.domain.models import SessionStatus
from issue_orchestrator.domain.session_kind import SessionKind
from issue_orchestrator.domain.state_machines.issue_machine import IssueState
from issue_orchestrator.observation.observation import SessionObservationResult
from tests.unit.control.liveness_doubles import RecordingEscalation, liveness_owner
from tests.unit.session_run_helpers import make_session_run_assets
from tests.unit.test_orchestrator import (
    create_issue,
    create_session,
    create_test_orchestrator,
    track_session,
)


def _rework_session(number: int):
    worktree = Path(tempfile.mkdtemp(prefix="io-rework-"))
    session = create_session(create_issue(number), worktree_path=worktree, task=SessionKind.REWORK)
    return replace(
        session,
        terminal_id=f"rework-{number}",
        run_assets=make_session_run_assets(worktree, session_name=f"rework-{number}"),
    )


@pytest.fixture
def engine(sample_config):
    """An engine with a pr_pending issue #450 under rework and an in-progress #451."""
    orchestrator = create_test_orchestrator(sample_config)
    escalation = RecordingEscalation()
    owner = liveness_owner(escalation=escalation)
    orchestrator.deps = replace(
        orchestrator.deps, action_liveness=replace(orchestrator.deps.action_liveness, owner=owner)
    )

    rework = _rework_session(450)
    coding = create_session(create_issue(451))
    machines = orchestrator.deps.state_machine_manager
    published = machines.get_issue_machine(rework.issue)
    published.claim()
    published.start()
    published.pr_created(data={"pr_url": "https://github.com/o/r/pull/9450"})
    worked = machines.get_issue_machine(coding.issue)
    worked.claim()
    worked.start()
    track_session(orchestrator, rework)
    track_session(orchestrator, coding)

    decisions = {
        rework.terminal_id: SessionDecision(status=SessionStatus.NEEDS_HUMAN, reason="asks a person"),
        coding.terminal_id: SessionDecision(status=SessionStatus.BLOCKED, reason="cannot proceed"),
    }

    def decide_outcome(_obs, _worktree, _number, _title, terminal_id, *_args, **_kwargs):
        return decisions[terminal_id]

    with (
        patch.object(
            orchestrator.observer,
            "observe_session",
            return_value=SessionObservationResult.terminated(runtime_minutes=3.0),
        ),
        patch.object(
            orchestrator.deps.session_controller, "decide_outcome", side_effect=decide_outcome
        ),
    ):
        yield orchestrator, owner, escalation, rework, coding


def test_rework_needs_human_on_pr_pending_issue_leaves_other_work_proceeding(engine):
    """The rework's ask is not the issue's lifecycle: #450 stays pr_pending, #451 completes."""
    orchestrator, owner, escalation, rework, coding = engine

    orchestrator._process_active_sessions()

    machines = orchestrator.deps.state_machine_manager
    assert machines.get_issue_machine(rework.issue).get_state() is IssueState.PR_PENDING
    assert machines.get_issue_machine(coding.issue).get_state() is IssueState.BLOCKED
    assert orchestrator.state.active_sessions == []
    # Nothing failed, so nothing was confined, parked or escalated.
    assert owner.admit(CompletionContainment.key(rework)).row is None
    assert escalation.blocks == []


def test_a_completion_that_raises_is_confined_to_its_session(engine):
    """#8000's class: one session's completion failure parks that session, not the tick."""
    orchestrator, owner, escalation, rework, coding = engine
    handler = orchestrator._completion_handler
    real_finalize = handler.finalize_terminal_outcome

    def finalize(session, *args, **kwargs):
        if session.terminal_id == rework.terminal_id:
            raise RuntimeError("Can't trigger event needs_human from state pr_pending!")
        return real_finalize(session, *args, **kwargs)

    with patch.object(handler, "finalize_terminal_outcome", side_effect=finalize):
        orchestrator._process_active_sessions()

    # The sibling completed in the same pass.
    machines = orchestrator.deps.state_machine_manager
    assert machines.get_issue_machine(coding.issue).get_state() is IssueState.BLOCKED
    assert orchestrator.state.active_sessions == []
    # The failed completion parked on its own issue and escalated to a person.
    parked = owner.admit(CompletionContainment.key(rework))
    assert not parked.admitted
    assert parked.row is not None and "pr_pending" in parked.row.last_reason
    assert [row.key.escalation_issue for row in escalation.committed_blocks] == [450]


def test_the_tick_goes_on_to_planning_after_a_confined_completion(engine):
    """The same pass inside a real tick: planning runs in the tick the failure happened."""
    orchestrator, _owner, _escalation, rework, _coding = engine
    handler = orchestrator._completion_handler
    planned: list[bool] = []

    def finalize(session, *args, **kwargs):
        raise RuntimeError(f"completion of {session.terminal_id} failed")

    with (
        patch.object(handler, "finalize_terminal_outcome", side_effect=finalize),
        patch.object(
            orchestrator._plan_applier,
            "clear_discovered_facts",
            side_effect=lambda _tick: planned.append(True),
        ),
    ):
        orchestrator.tick()

    assert planned, "the tick aborted before planning"


def test_a_decision_that_raises_is_confined_in_a_synchronous_tick(engine):
    """r1 F1: the inline dispatcher hands a decide error back to the apply boundary.

    It used to raise straight out of ``dispatch``, before any containment, so
    the next terminated session was never dispatched and planning never ran.
    """
    orchestrator, owner, escalation, rework, coding = engine
    controller = orchestrator.deps.session_controller
    real_decide = controller.decide_outcome.side_effect
    planned: list[bool] = []

    def decide_outcome(obs, worktree, number, title, terminal_id, *args, **kwargs):
        if terminal_id == rework.terminal_id:
            raise FileNotFoundError("run dir missing")
        return real_decide(obs, worktree, number, title, terminal_id, *args, **kwargs)

    controller.decide_outcome.side_effect = decide_outcome
    with patch.object(
        orchestrator._plan_applier,
        "clear_discovered_facts",
        side_effect=lambda _tick: planned.append(True),
    ):
        orchestrator.tick()

    assert planned, "the tick aborted before planning"
    machines = orchestrator.deps.state_machine_manager
    assert machines.get_issue_machine(coding.issue).get_state() is IssueState.BLOCKED
    # The failed decision's session stays active and is retried after its backoff.
    assert orchestrator.state.active_sessions == [rework]
    row = owner.admit(CompletionContainment.key(rework)).row
    assert row is not None and "run dir missing" in row.last_reason and not row.parked
    assert escalation.blocks == []
