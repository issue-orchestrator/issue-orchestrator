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


def _retry(authority_run: SessionRunIdentity | None) -> PendingValidationRetry:
    return PendingValidationRetry(
        issue_number=6410,
        issue_title="Investigate stranded failure",
        agent_label="agent:tech-lead",
        worktree_path="/tmp/worktree-6410",
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
    """Only a retry resuming a run that HAD authority reads those artifacts."""
    sample_config.tech_lead_review_on_failure = True
    sample_config.tech_lead_review_agent = "agent:tech-lead"
    state.pending_validation_retries.append(_retry(None))

    assert _held(state, sample_config) == frozenset()
