"""State-table tests for ``ReviewExchangeResumeDecision``.

The whole point of moving cache/retry policy behind one named owner
is so the matrix is enumerable. This module enumerates it. Adding a
new ``(status, reason)`` row in ``decide()`` should require adding a
row here too — the parametrized table fails closed if a new pair
goes through without a documented decision.

These are pure-domain tests: no I/O, no mocks. Every input is a
``ResumeFacts`` literal; every assertion is on the returned
``ResumeDecision`` variant. If a future refactor introduces a
filesystem dependency on the helper, that's a regression.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.review_exchange_resume import (
    REASON_CODER_NO_COMPLETION,
    REASON_CODER_PROTOCOL_ERROR,
    REASON_MAX_ROUNDS_EXCEEDED,
    REASON_REVIEWER_NO_COMPLETION,
    REASON_REVIEWER_OK,
    REASON_REVIEWER_REPORTS_NO_PROGRESS,
    ResumeDecision,
    ResumeFacts,
    decide,
    is_no_completion_reason,
)


# ---------------------------------------------------------------------------
# Helpers — keep test rows readable
# ---------------------------------------------------------------------------


def _facts(
    *,
    status: str | None = "ok",
    reason: str | None = REASON_REVIEWER_OK,
    cached_head_sha: str | None = "HEAD_X",
    cached_validation_passed: bool | None = True,
    current_head_sha: str | None = "HEAD_X",
    current_validation_failed: bool = False,
    no_completion_count: int = 0,
    require_validation: bool = True,
) -> ResumeFacts:
    return ResumeFacts(
        status=status,
        reason=reason,
        cached_head_sha=cached_head_sha,
        cached_validation_passed=cached_validation_passed,
        current_head_sha=current_head_sha,
        current_validation_failed=current_validation_failed,
        no_completion_count=no_completion_count,
        require_validation=require_validation,
    )


# ---------------------------------------------------------------------------
# Empty / malformed cache
# ---------------------------------------------------------------------------


class TestNoCacheAndInvalid:
    def test_no_cache_when_status_and_reason_both_absent(self) -> None:
        assert decide(_facts(status=None, reason=None)) is ResumeDecision.NO_CACHE

    def test_invalid_when_status_known_but_reason_unknown(self) -> None:
        # Forward-compat: a future status the helper hasn't been
        # taught about must NOT be silently treated as reusable.
        assert decide(_facts(reason="unrecognized_reason")) is ResumeDecision.INVALID_SUMMARY

    def test_invalid_when_reason_known_but_status_unrecognized(self) -> None:
        assert decide(_facts(status="weird_status", reason=REASON_REVIEWER_OK)) is ResumeDecision.INVALID_SUMMARY

    def test_invalid_when_pair_exists_but_doesnt_match_known_combos(self) -> None:
        # status=ok with a non-OK reason is invalid (forward-compat
        # / corruption); the helper refuses to guess.
        assert decide(_facts(status="ok", reason=REASON_REVIEWER_NO_COMPLETION)) is ResumeDecision.INVALID_SUMMARY


# ---------------------------------------------------------------------------
# Reuse approval (OK at current head)
# ---------------------------------------------------------------------------


class TestReuseApproval:
    def test_ok_at_current_head_is_reusable(self) -> None:
        assert decide(_facts()) is ResumeDecision.REUSE_APPROVAL

    def test_ok_works_when_validation_not_required(self) -> None:
        # Even without require_validation, an OK at the current head
        # is the cacheable case. (Belt-and-suspenders: most production
        # paths set require_validation=True; this test pins behavior
        # for dev/test paths that don't.)
        assert decide(_facts(require_validation=False)) is ResumeDecision.REUSE_APPROVAL

    def test_ok_at_current_head_with_no_current_validation_when_not_required(self) -> None:
        assert decide(_facts(
            require_validation=False,
            current_head_sha=None,
            cached_head_sha=None,
        )) is ResumeDecision.REUSE_APPROVAL


# ---------------------------------------------------------------------------
# Reuse halt (deterministic terminal outcomes)
# ---------------------------------------------------------------------------


class TestReuseHalt:
    @pytest.mark.parametrize("reason", [
        REASON_REVIEWER_REPORTS_NO_PROGRESS,
        REASON_MAX_ROUNDS_EXCEEDED,
    ])
    def test_stopped_outcomes_reuse_halt(self, reason: str) -> None:
        assert decide(_facts(status="stopped", reason=reason)) is ResumeDecision.REUSE_HALT

    def test_coder_protocol_error_at_current_head_is_reuse_halt(self) -> None:
        # Critical: protocol_error is NOT a no-completion. It's a
        # deterministic "the agent broke protocol" — won't fix itself
        # by retrying on the same head. Reuse_halt drives an
        # immediate escalation rather than the no-completion budget
        # cycle. Pre-PR #6271 this case had no path to halt and
        # would respawn forever (review feedback on PR #6270).
        assert decide(_facts(
            status="error",
            reason=REASON_CODER_PROTOCOL_ERROR,
        )) is ResumeDecision.REUSE_HALT


# ---------------------------------------------------------------------------
# Count-and-retry (no-completion budget)
# ---------------------------------------------------------------------------


class TestCountNoCompletionAndRetry:
    @pytest.mark.parametrize("reason", [
        REASON_REVIEWER_NO_COMPLETION,
        REASON_CODER_NO_COMPLETION,
    ])
    def test_no_completion_at_current_head_is_count_and_retry(self, reason: str) -> None:
        assert decide(_facts(
            status="error",
            reason=reason,
        )) is ResumeDecision.COUNT_NO_COMPLETION_AND_RETRY

    def test_no_completion_with_low_budget_is_still_retry(self) -> None:
        # The decide() helper does NOT consult budget thresholds.
        # The caller compares ``no_completion_count`` against the
        # configured ``max_consecutive_review_exchange_failures``
        # and decides retry vs escalate. This test pins that
        # responsibility split.
        assert decide(_facts(
            status="error",
            reason=REASON_REVIEWER_NO_COMPLETION,
            no_completion_count=2,
        )) is ResumeDecision.COUNT_NO_COMPLETION_AND_RETRY

    def test_no_completion_at_threshold_is_still_count_and_retry(self) -> None:
        # Even at the threshold, the helper returns the same Decision.
        # Threshold comparison and the resulting needs-human halt
        # belong to the caller, not the helper. Keeping decide()
        # threshold-blind makes the helper pure.
        assert decide(_facts(
            status="error",
            reason=REASON_REVIEWER_NO_COMPLETION,
            no_completion_count=3,
        )) is ResumeDecision.COUNT_NO_COMPLETION_AND_RETRY


# ---------------------------------------------------------------------------
# Stale (head/validation invalidates cache regardless of status)
# ---------------------------------------------------------------------------


class TestIgnoreStale:
    def test_head_drift_invalidates_ok_summary(self) -> None:
        # The OG bug pattern this helper exists to make explicit:
        # an OK at OLD_SHA when current is NEW_SHA must NOT reuse,
        # even though every other field looks valid.
        assert decide(_facts(
            cached_head_sha="OLD_SHA",
            current_head_sha="NEW_SHA",
        )) is ResumeDecision.IGNORE_STALE

    def test_head_drift_invalidates_no_completion_summary(self) -> None:
        # Stale check fires BEFORE the no-completion classification —
        # otherwise we'd count failures from a stale commit toward
        # the current commit's budget.
        assert decide(_facts(
            status="error",
            reason=REASON_REVIEWER_NO_COMPLETION,
            cached_head_sha="OLD_SHA",
            current_head_sha="NEW_SHA",
        )) is ResumeDecision.IGNORE_STALE

    def test_missing_current_head_under_require_validation_is_stale(self) -> None:
        # No way to verify the cache applies to "now" if we don't
        # know what "now" is. Refuse to reuse.
        assert decide(_facts(
            current_head_sha=None,
            require_validation=True,
        )) is ResumeDecision.IGNORE_STALE

    def test_unvalidated_cache_under_require_validation_is_stale(self) -> None:
        # Legacy summary with no head_sha embedding and no readable
        # sibling validation-record. cached_validation_passed=None.
        # Caller has set require_validation=True, so we cannot trust
        # the cache.
        assert decide(_facts(
            cached_head_sha=None,
            cached_validation_passed=None,
            require_validation=True,
        )) is ResumeDecision.IGNORE_STALE

    def test_failed_cached_validation_under_require_validation_is_stale(self) -> None:
        assert decide(_facts(
            cached_validation_passed=False,
            require_validation=True,
        )) is ResumeDecision.IGNORE_STALE

    def test_current_validation_failed_invalidates_cached_approval(self) -> None:
        # Same SHA, cache says approved, but the orchestrator's
        # current validation flipped to failed. The cached approval
        # no longer holds — typically because the agent's validation
        # was a flake or its own subsequent run regressed something.
        assert decide(_facts(
            current_validation_failed=True,
        )) is ResumeDecision.IGNORE_STALE

    def test_stale_takes_precedence_over_protocol_error(self) -> None:
        # protocol_error normally → REUSE_HALT, but if the head has
        # moved we should re-evaluate at the new head (the new
        # commit might fix the protocol issue).
        assert decide(_facts(
            status="error",
            reason=REASON_CODER_PROTOCOL_ERROR,
            cached_head_sha="OLD_SHA",
            current_head_sha="NEW_SHA",
        )) is ResumeDecision.IGNORE_STALE


# ---------------------------------------------------------------------------
# is_no_completion_reason — single source of truth
# ---------------------------------------------------------------------------


class TestNoCompletionClassifier:
    """``is_no_completion_reason`` is the public classifier the
    retry-counter uses. Pinning it here prevents drift between the
    decide() table and the counter."""

    @pytest.mark.parametrize("reason", [
        REASON_REVIEWER_NO_COMPLETION,
        REASON_CODER_NO_COMPLETION,
    ])
    def test_no_completion_reasons_are_classified(self, reason: str) -> None:
        assert is_no_completion_reason(reason) is True

    @pytest.mark.parametrize("reason", [
        REASON_REVIEWER_OK,
        REASON_REVIEWER_REPORTS_NO_PROGRESS,
        REASON_MAX_ROUNDS_EXCEEDED,
        REASON_CODER_PROTOCOL_ERROR,
        "made_up_reason",
        "",
    ])
    def test_other_reasons_are_not_no_completion(self, reason: str) -> None:
        assert is_no_completion_reason(reason) is False

    def test_none_is_not_no_completion(self) -> None:
        assert is_no_completion_reason(None) is False


# ---------------------------------------------------------------------------
# Full matrix — all known (status, reason) pairs map to a documented variant
# ---------------------------------------------------------------------------


# This is the keystone test. The known-pairs frozen-set inside
# ``decide`` must stay in lockstep with this table; if a future
# change adds a new pair to the helper without adding a row here,
# the test fails on the first run (UNKNOWN_PAIR) — forcing the
# refactor author to think through the new cell.
_MATRIX_AT_CURRENT_HEAD: list[tuple[str, str, ResumeDecision]] = [
    ("ok", REASON_REVIEWER_OK, ResumeDecision.REUSE_APPROVAL),
    ("stopped", REASON_REVIEWER_REPORTS_NO_PROGRESS, ResumeDecision.REUSE_HALT),
    ("stopped", REASON_MAX_ROUNDS_EXCEEDED, ResumeDecision.REUSE_HALT),
    ("error", REASON_REVIEWER_NO_COMPLETION, ResumeDecision.COUNT_NO_COMPLETION_AND_RETRY),
    ("error", REASON_CODER_NO_COMPLETION, ResumeDecision.COUNT_NO_COMPLETION_AND_RETRY),
    ("error", REASON_CODER_PROTOCOL_ERROR, ResumeDecision.REUSE_HALT),
]


@pytest.mark.parametrize(("status", "reason", "expected"), _MATRIX_AT_CURRENT_HEAD)
def test_matrix_at_current_head_maps_to_expected_decision(
    status: str, reason: str, expected: ResumeDecision,
) -> None:
    """For every known ``(status, reason)`` at the current head,
    ``decide`` returns the documented variant. Adding a new row to
    ``_KNOWN_STATUS_REASON_PAIRS`` in ``review_exchange_resume.py``
    without adding a row here means the new cell isn't reasoned
    about — the test will catch it because ``decide`` will return
    ``INVALID_SUMMARY`` for the unknown pair and the expected
    Decision won't match.
    """
    assert decide(_facts(status=status, reason=reason)) is expected


def test_matrix_covers_every_known_pair_in_decide() -> None:
    """Coverage guard: the test matrix must enumerate every pair the
    decide() helper recognizes. Adding a new pair to ``decide`` without
    adding a row to ``_MATRIX_AT_CURRENT_HEAD`` breaks this assertion.
    """
    from issue_orchestrator.domain import review_exchange_resume as rer

    matrix_pairs = {(s, r) for s, r, _ in _MATRIX_AT_CURRENT_HEAD}
    known_pairs = rer._KNOWN_STATUS_REASON_PAIRS  # noqa: SLF001
    missing = known_pairs - matrix_pairs
    extra = matrix_pairs - known_pairs
    assert not missing, (
        f"new (status, reason) pair(s) added to decide() without test "
        f"coverage in _MATRIX_AT_CURRENT_HEAD: {sorted(missing)}"
    )
    assert not extra, (
        f"test matrix references pair(s) decide() doesn't recognize: "
        f"{sorted(extra)}"
    )
