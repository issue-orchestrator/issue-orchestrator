"""A queued validation retry's launch authority survives a restart (#7273).

The retry names the run whose `TechLeadLaunchAuthority` it inherits. That name
is only useful if it is still there after the orchestrator comes back, because
a restart is one of the two ways a tech-lead investigation's retry reaches the
launcher -- and a retry that has forgotten its source run relaunches into a
completion the orchestrator must reject as `missing_authority`.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.models import PendingValidationRetry
from issue_orchestrator.domain.pending_work import PendingWorkClaim, PendingWorkKind
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.domain.session_run import SessionRunIdentity
from issue_orchestrator.execution.pending_work_codec import decode_claim, encode_claim

SOURCE = SessionRunIdentity(
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


def _round_trip(retry: PendingValidationRetry) -> PendingValidationRetry:
    claim = PendingWorkClaim(
        kind=PendingWorkKind.VALIDATION_RETRY,
        request=retry,
    )
    restored = decode_claim(encode_claim(claim)).request
    assert isinstance(restored, PendingValidationRetry)
    return restored


def test_the_source_run_survives_the_round_trip() -> None:
    assert _round_trip(_retry(SOURCE)).authority_run == SOURCE


def test_a_retry_that_never_had_one_stays_without_one() -> None:
    """Every non-tech-lead retry, and every retry queued before #7273."""
    assert _round_trip(_retry(None)).authority_run is None


def test_a_payload_that_is_not_an_object_is_refused() -> None:
    """Better to fail loudly than to relaunch having forgotten the grant."""
    claim = PendingWorkClaim(
        kind=PendingWorkKind.VALIDATION_RETRY,
        request=_retry(SOURCE),
    )
    payload = encode_claim(claim)
    payload["request"]["authority_run"] = "run-original"

    with pytest.raises(Exception, match="authority_run"):
        decode_claim(payload)
