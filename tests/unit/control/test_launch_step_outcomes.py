"""Every launch disposition has one plan-step outcome (#7455, #7461 review)."""

from __future__ import annotations

import pytest

from issue_orchestrator.control.session_launch_types import (
    STEP_OUTCOME_BY_DISPOSITION,
    LaunchDisposition,
    LaunchResult,
    LaunchStep,
    LaunchStepOutcome,
)
from issue_orchestrator.domain.host_rate_limit import HostRateLimit


def test_every_disposition_is_mapped() -> None:
    """A new disposition must be classified, never defaulted to NOT_LAUNCHED."""
    assert set(STEP_OUTCOME_BY_DISPOSITION) == set(LaunchDisposition)


#: Retained with nothing spent: a wait, never a failed action.
_WAITS = {
    LaunchDisposition.PROVIDER_DEFERRED,
    LaunchDisposition.HOST_RATE_LIMITED,
    LaunchDisposition.CLAIM_UNRECORDED,
    LaunchDisposition.HELD_BY_RECOVERY,
    LaunchDisposition.SUBJECT_BUSY,
}
#: An attempt that did not start: the only failures.
_FAILURES = {
    LaunchDisposition.RETRYABLE_FAILURE,
    LaunchDisposition.PERMANENT_FAILURE,
    LaunchDisposition.EXISTING_TERMINAL,
}


def _result(disposition: LaunchDisposition) -> LaunchResult:
    from datetime import UTC, datetime

    if disposition is LaunchDisposition.HOST_RATE_LIMITED:
        limit = HostRateLimit(resets_at=datetime.now(UTC), kind="primary", resource="core")
        return LaunchResult.host_rate_limited("rate limited", limit)
    if disposition is LaunchDisposition.EXISTING_TERMINAL:
        return LaunchResult.terminal_already_running("issue-7")
    return LaunchResult(None, False, disposition.value, disposition=disposition)


@pytest.mark.parametrize(
    "disposition", sorted(set(LaunchDisposition) - {LaunchDisposition.LAUNCHED}, key=lambda d: d.value)
)
def test_a_retained_unspent_launch_is_a_wait_and_only_an_attempt_is_a_failure(
    disposition: LaunchDisposition,
) -> None:
    step = LaunchStep.of_result(_result(disposition))

    if disposition in _WAITS:
        assert step.outcome is LaunchStepOutcome.WAITING
    elif disposition in _FAILURES:
        assert step.outcome is LaunchStepOutcome.NOT_LAUNCHED
    else:
        assert disposition is LaunchDisposition.WITHDRAWN
        assert step.outcome is LaunchStepOutcome.WITHDRAWN
