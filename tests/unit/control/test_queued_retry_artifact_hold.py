"""A queued investigation retry keeps the artifacts it is going to read (#7273).

The hold exists because a failed session records its cleanup in the same pass
that records the failure, while the tech-lead work that reads those artifacts
runs on a later tick. A validation retry is that later tick -- and it was not
one of the things the hold looked at, so a retry waiting to resume an
investigation could find its inputs already deleted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.control.tech_lead_artifact_retention import (
    tech_lead_problem_artifact_hold_issue_numbers,
)
from issue_orchestrator.domain.models import OrchestratorState, PendingValidationRetry
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.domain.session_run import SessionRunIdentity

ORIGINAL = SessionRunIdentity(
    session_name="issue-6410", run_id="run-original", started_at="2026-09-18T00:00:00Z"
)


def _retry(
    authority_run: SessionRunIdentity | None,
    *,
    worktree_path: str = "/tmp/wt/repo-tech-lead-6410-abcdef123456",
) -> PendingValidationRetry:
    """A queued retry. The CHECKOUT NAME is what says it is an investigation.

    The hold used to key on ``authority_run``, which is read back off a run
    manifest -- so a manifest that could not be read released the hold on
    exactly the retry that most needed it (round 1 finding 4). The checkout
    path is a durable fact that no read can lose.
    """
    return PendingValidationRetry(
        issue_number=6410,
        issue_title="Investigate stranded failure",
        agent_label="agent:tech-lead",
        worktree_path=worktree_path,
        branch_name="tech-lead-investigation-6410-abcdef123456",
        original_prompt="Investigate issue #6410",
        validation_error="boom",
        validation_error_file=None,
        retry_count=1,
        source_task=TaskKind.CODE,
        validation_cmd="make test",
        authority_run=authority_run,
    )


@pytest.fixture
def state() -> OrchestratorState:
    return OrchestratorState()


def _held(state: OrchestratorState, config) -> frozenset[int]:
    return tech_lead_problem_artifact_hold_issue_numbers(state, config)


def test_a_queued_investigation_retry_holds_its_artifacts(
    state: OrchestratorState, sample_config
) -> None:
    sample_config.tech_lead_review_on_failure = True
    sample_config.tech_lead_review_agent = "agent:tech-lead"
    state.pending_validation_retries.append(_retry(ORIGINAL))

    assert 6410 in _held(state, sample_config)


def test_switching_tech_lead_review_off_does_not_release_a_queued_retry(
    state: OrchestratorState, sample_config
) -> None:
    """The configuration decides what STARTS, not what already queued keeps.

    Switching the feature off used to release every hold at once, discarding
    the inputs of retries that were already waiting to run.
    """
    sample_config.tech_lead_review_on_failure = False
    state.pending_validation_retries.append(_retry(ORIGINAL))

    assert 6410 in _held(state, sample_config)


def test_an_ordinary_retry_holds_nothing(
    state: OrchestratorState, sample_config
) -> None:
    """An ordinary coding retry reads no tech-lead artifacts.

    Its checkout is the issue's own worktree, which names no investigation --
    and that, not the presence of an authority row, is what distinguishes it.
    """
    sample_config.tech_lead_review_on_failure = True
    sample_config.tech_lead_review_agent = "agent:tech-lead"
    state.pending_validation_retries.append(
        _retry(None, worktree_path="/tmp/wt/repo-6410")
    )

    assert _held(state, sample_config) == frozenset()


def test_an_investigation_retry_holds_even_with_no_authority_run(
    state: OrchestratorState, sample_config
) -> None:
    """The hold survives a run manifest this build could not read.

    That is the case where the artifacts matter MOST: the retry cannot name its
    launch authority, so an operator is going to have to look at what the run
    left behind (round 1 finding 4).
    """
    sample_config.tech_lead_review_on_failure = True
    sample_config.tech_lead_review_agent = "agent:tech-lead"
    state.pending_validation_retries.append(_retry(None))

    assert _held(state, sample_config) == frozenset({6410})


def test_switching_tech_lead_review_off_does_not_release_an_ACTIVE_session(
    state: OrchestratorState, sample_config
) -> None:
    """The queued case was fixed; the ACTIVE one was still behind the gate.

    Once the retry launches it leaves `pending_validation_retries` and becomes
    an active session. Disabling review then returned an empty hold set, so
    cleanup could delete the failed-run artifacts the session was reading right
    then (round 14 finding 4).
    """
    from unittest.mock import MagicMock

    from issue_orchestrator.domain.tech_lead_session import (
        TechLeadLaunchScope,
        TechLeadSessionFlavor,
    )

    sample_config.tech_lead_review_on_failure = False
    sample_config.tech_lead_review_agent = "agent:tech-lead"
    session = MagicMock()
    session.issue.number = 6410
    session.agent_label = "agent:backend"
    session.tech_lead_scope = TechLeadLaunchScope(
        flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION
    )
    state.active_sessions.append(session)

    assert 6410 in _held(state, sample_config)


def test_discovery_is_still_gated_on_the_configuration(
    state: OrchestratorState, sample_config
) -> None:
    """What the switch DOES decide: whether new investigations start."""
    from unittest.mock import MagicMock

    sample_config.tech_lead_review_on_failure = False
    sample_config.tech_lead_review_agent = "agent:tech-lead"
    failure = MagicMock()
    failure.issue_number = 7777
    state.discovered_failures.append(failure)

    assert 7777 not in _held(state, sample_config)

