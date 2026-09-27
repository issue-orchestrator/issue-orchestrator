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
from issue_orchestrator.domain.session_kind import SessionKind
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
        # Only a tech-lead retry inherits launch authority (#7347); one that
        # names none is read as the pre-#7273 "code" stamp it was queued under.
        source_kind=SessionKind.TECH_LEAD if authority_run is not None else SessionKind.CODE,
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


def test_a_tech_lead_retry_round_trips_as_a_tech_lead() -> None:
    """The retry relaunches AS its source kind (#7347), so the kind must survive."""
    restored = _round_trip(_retry(SOURCE))
    assert restored.source_kind is SessionKind.TECH_LEAD


def test_a_reworks_retry_round_trips_as_rework() -> None:
    from dataclasses import replace

    rework = replace(_retry(None), source_kind=SessionKind.REWORK, agent_label="agent:web")
    assert _round_trip(rework).source_kind is SessionKind.REWORK


def test_a_pre_7347_tech_lead_retry_decodes_as_a_tech_lead() -> None:
    """Before #7347 a tech-lead run was stamped "code", and so was its retry.

    Only a tech-lead retry carries an ``authority_run``, so a queued "code"
    retry that names one is a tech lead's -- read without guessing.
    """
    payload = encode_claim(
        PendingWorkClaim(kind=PendingWorkKind.VALIDATION_RETRY, request=_retry(SOURCE))
    )
    assert payload["request"]["source_task"] == "tech-lead"
    payload["request"]["source_task"] = "code"

    restored = decode_claim(payload).request

    assert isinstance(restored, PendingValidationRetry)
    assert restored.source_kind is SessionKind.TECH_LEAD
    assert restored.authority_run == SOURCE


def test_only_a_tech_lead_retry_may_inherit_authority() -> None:
    from dataclasses import replace

    with pytest.raises(ValueError, match="cannot inherit tech-lead launch authority"):
        replace(_retry(SOURCE), source_kind=SessionKind.REWORK)
    with pytest.raises(ValueError, match="names no launch authority"):
        replace(_retry(SOURCE), authority_run=None)
    # A tech-lead retry already refused for a recovery inconsistency is held,
    # not rejected: its checkout and artifact holds still protect the work.
    held = replace(_retry(SOURCE), authority_run=None, recovery_error="authority lost")
    assert held.source_kind is SessionKind.TECH_LEAD


@pytest.mark.parametrize("kind", [SessionKind.REVIEW, SessionKind.RETROSPECTIVE_REVIEW, SessionKind.HISTORICAL])
def test_work_that_makes_no_launched_commits_is_never_retried(kind: SessionKind) -> None:
    from dataclasses import replace

    with pytest.raises(ValueError, match="only a launched session that makes commits"):
        replace(_retry(None), source_kind=kind)


def test_a_reworks_retry_keeps_its_pr_and_cycle_across_a_restart() -> None:
    from dataclasses import replace

    rework = replace(
        _retry(None), source_kind=SessionKind.REWORK, agent_label="agent:web",
        pr_number=456, rework_cycle=2,
    )
    restored = _round_trip(rework)
    assert (restored.pr_number, restored.rework_cycle) == (456, 2)


def test_a_pre_7347_retry_payload_has_no_pr_or_cycle_and_is_not_guessed() -> None:
    payload = encode_claim(
        PendingWorkClaim(kind=PendingWorkKind.VALIDATION_RETRY, request=_retry(None))
    )
    payload["request"].pop("pr_number")
    payload["request"].pop("rework_cycle")

    restored = decode_claim(payload).request

    assert isinstance(restored, PendingValidationRetry)
    assert (restored.pr_number, restored.rework_cycle) == (None, None)
