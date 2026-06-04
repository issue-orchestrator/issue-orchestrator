"""Resume-decision policy for cached review-exchange summaries.

Owns the single-named-place answer to: "given the facts persisted by a
prior review-exchange run, what should the next orchestrator tick do?"

Pre-this-module the answer was inferred at three call sites — the
summary writer (which fields to omit), the cache loader (which paths
to walk for validation), and the no-completion counter (which reasons
count toward the budget). Those three sites kept drifting against
each other (PR #6267 / #6268 / #6270 review history): every patch
fixed one cell of the implicit ``(status, reason, head, validation,
budget)`` matrix and broke another. This module makes the matrix one
explicit table and gives every consumer the same answer.

Inputs (``ResumeFacts``) are pure persisted facts. Outputs
(``ResumeDecision``) name the action the caller should take. Adding
a new ``(status, reason)`` cell becomes a single row in ``decide``
plus a row in the parametrized state-table test.

This module is sandbox-clean: no I/O, no logging side effects, no
imports from outside ``domain``. Production callers translate
filesystem state into ``ResumeFacts`` and dispatch on the returned
``ResumeDecision``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ResumeDecision(Enum):
    """What the next tick should do with a cached review-exchange summary.

    Each variant names exactly one possible action. The variants are
    deliberately distinct so callers cannot conflate them — e.g.
    ``IGNORE_STALE`` (head moved on; spawn fresh) is structurally
    different from ``COUNT_NO_COMPLETION_AND_RETRY`` (timeout; counts
    toward the no-completion budget; spawn fresh until threshold)
    and from ``REUSE_HALT`` (deterministic terminal outcome; reuse
    the verdict, don't spawn). Pre-PR #6271 these all collapsed onto
    "load_existing_review_exchange_outcome returns None" or "returns
    cached outcome" with the caller forced to reinfer policy from
    status / reason / file presence.
    """

    REUSE_APPROVAL = "reuse_approval"
    """Cached ``status=ok reason=reviewer_ok`` at the current head.

    The verdict stands; the caller proceeds with publish/PR creation
    using the cached outcome. No new exchange.
    """

    REUSE_HALT = "reuse_halt"
    """Cached deterministic terminal outcome the caller should not
    retry. Today: ``status=stopped`` (max_rounds_exceeded,
    reviewer_reports_no_progress) and ``status=error
    reason=coder_protocol_error``.

    The exchange tried hard and gave up for a reason that won't
    spontaneously change on the same head. The caller surfaces a
    halting failure / needs-human escalation, but does NOT spawn
    another exchange.
    """

    COUNT_NO_COMPLETION_AND_RETRY = "count_no_completion_and_retry"
    """Cached ``status=error reason=*_no_completion`` at the current
    head. The agent never produced a verdict — the failure is
    "didn't get an answer," not "the answer was bad." The caller
    increments the no-completion budget (PR #6267) and, if under
    threshold, spawns a fresh exchange. Above threshold, the caller
    halts with a needs-human escalation.

    This variant is the only one that asks the caller to consult
    a budget counter. ``REUSE_HALT`` is unconditional halt;
    ``IGNORE_STALE`` is unconditional spawn-fresh.
    """

    IGNORE_STALE = "ignore_stale"
    """Cache exists but does not apply to the current state.

    Covers: cached head differs from current head (new commit since
    last review); cache predates the scratch-reset boundary; cache
    was unvalidated when require_validation is on; current
    validation is missing or has explicitly failed. The cache is
    not authoritative for the current state; the caller spawns a
    fresh exchange and the budget counter is NOT incremented (this
    isn't a no-completion failure — it's a context change).
    """

    NO_CACHE = "no_cache"
    """No prior summary exists for this session within the scratch-
    reset boundary. The caller spawns a fresh exchange. Budget is
    not affected.
    """

    INVALID_SUMMARY = "invalid_summary"
    """Summary exists but is malformed or carries an unrecognized
    ``(status, reason)``. Defensive variant — a forward-compat case
    or a corrupted file. The caller should treat this as
    ``NO_CACHE`` for spawning purposes (don't trust corrupted
    state) and log loudly so an operator can investigate.
    """


@dataclass(frozen=True)
class ResumeFacts:
    """Pure facts about the cached summary and current state.

    Every field is a fact, not a decision. The decision falls out of
    ``decide(facts)``. Callers translate filesystem state into
    ``ResumeFacts`` (cache loader reads summary.json + current
    validation-record.json) and consume the returned
    ``ResumeDecision``.

    ``status`` / ``reason`` come from the cached summary verbatim.
    Both are ``None`` when no summary exists for this session.

    ``cached_head_sha`` is the commit the cached summary covers, read
    from the summary's embedded ``head_sha`` (post PR #6271 self-
    describing summary) when present, otherwise from a sibling
    validation-record.json (legacy).

    ``cached_validation_passed`` is True when the cached summary's
    backing validation record was readable and reported ``passed:
    true``; False when it was readable and reported ``passed: false``;
    None when the cache cannot establish validation status (legacy
    summary with no head_sha embedding and no sibling record).

    ``current_head_sha`` is the head_sha from the validation record
    the orchestrator is currently considering (the coder's run-dir
    record). None when the orchestrator has no current validation.

    ``current_validation_failed`` is True when the orchestrator's
    current validation record is readable and reports ``passed:
    false``; False otherwise (passed or unknown). Used to invalidate
    a cached approval at the same SHA when validation flipped.

    ``no_completion_count`` is the consecutive ``*_no_completion``
    streak observed (PR #6267 budget). The decide function consults
    it indirectly by classifying as ``COUNT_NO_COMPLETION_AND_RETRY``;
    the caller compares against ``max_consecutive_review_exchange_failures``
    and decides retry vs escalate.

    ``require_validation`` mirrors the
    ``review_exchange_require_validation`` config flag. When False,
    head/validation gating is relaxed.
    """

    status: str | None
    reason: str | None
    cached_head_sha: str | None
    cached_validation_passed: bool | None
    current_head_sha: str | None
    current_validation_failed: bool
    no_completion_count: int
    require_validation: bool


# ---------------------------------------------------------------------------
# Reason constants — single source of truth for the matrix
# ---------------------------------------------------------------------------

STATUS_REVIEWER_OK = "ok"
"""Terminal status: reviewer approved."""

STATUS_REVIEWER_STOPPED = "stopped"
"""Terminal status: bounded exchange stopped before approval."""

STATUS_REVIEWER_ERROR = "error"
"""Terminal status: exchange ended with protocol/timeout failure."""

REASON_REVIEWER_OK = "reviewer_ok"
"""Status=ok terminal: the reviewer approved."""

REASON_REVIEWER_REPORTS_NO_PROGRESS = "reviewer_reports_no_progress"
"""Status=stopped terminal: max_no_progress threshold reached."""

REASON_MAX_ROUNDS_EXCEEDED = "max_rounds_exceeded"
"""Status=stopped terminal: ran the configured max number of rounds."""

REASON_REVIEWER_NO_COMPLETION = "reviewer_no_completion"
"""Status=error: reviewer round timed out without producing a verdict."""

REASON_CODER_NO_COMPLETION = "coder_no_completion"
"""Status=error: coder round timed out without producing a response."""

REASON_CODER_PROTOCOL_ERROR = "coder_protocol_error"
"""Status=error terminal: coder failed protocol (skipped coding-done,
malformed completion record). Distinct from no-completion: this
won't fix itself by retrying on the same head — escalates immediately."""


_NO_COMPLETION_REASONS: frozenset[str] = frozenset({
    REASON_REVIEWER_NO_COMPLETION,
    REASON_CODER_NO_COMPLETION,
})

_TERMINAL_HALT_REASONS: frozenset[str] = frozenset({
    REASON_REVIEWER_REPORTS_NO_PROGRESS,
    REASON_MAX_ROUNDS_EXCEEDED,
    REASON_CODER_PROTOCOL_ERROR,
})


def is_no_completion_reason(reason: str | None) -> bool:
    """Public: does this reason count toward the no-completion budget?

    The retry classifier in control uses this to filter recent
    summaries. Single source of truth so the writer, cache loader,
    and counter cannot drift.
    """
    return reason in _NO_COMPLETION_REASONS


# ---------------------------------------------------------------------------
# Decision function — the entire policy lives here
# ---------------------------------------------------------------------------


def decide(facts: ResumeFacts) -> ResumeDecision:
    """Map ``ResumeFacts`` to a ``ResumeDecision``.

    Pure function. No I/O. The full ``(status, reason, head_match,
    validation, budget)`` matrix lives here. Adding a new
    ``(status, reason)`` cell is a one-line addition; the
    parametrized state-table test pins the matrix.

    Decision precedence (top wins):

    1. No prior summary → ``NO_CACHE``
    2. Malformed / unknown ``(status, reason)`` → ``INVALID_SUMMARY``
    3. Stale: head moved, validation failed, validation required
       and missing → ``IGNORE_STALE``
    4. ``status=ok reason=reviewer_ok`` at current head → ``REUSE_APPROVAL``
    5. ``status=stopped`` or ``status=error reason=coder_protocol_error``
       at current head → ``REUSE_HALT``
    6. ``status=error reason=*_no_completion`` → ``COUNT_NO_COMPLETION_AND_RETRY``

    The order matters because (3) excludes any reuse before (4)–(6)
    fire. For example, an OK summary at OLD_SHA with current=NEW_SHA
    is ``IGNORE_STALE``, not ``REUSE_APPROVAL`` — even though it
    would otherwise match (4).
    """
    # No summary at all.
    if facts.status is None and facts.reason is None:
        return ResumeDecision.NO_CACHE

    # Unrecognized status/reason (forward-compat or corruption).
    if not _is_known(facts.status, facts.reason):
        return ResumeDecision.INVALID_SUMMARY

    # Staleness checks. These have to fire before reuse, otherwise an
    # OK at an old head looks reusable — exactly the bug class PR
    # #6271 is designed to prevent.
    if _is_stale(facts):
        return ResumeDecision.IGNORE_STALE

    if facts.status == "ok" and facts.reason == REASON_REVIEWER_OK:
        return ResumeDecision.REUSE_APPROVAL

    if facts.status == "stopped" or facts.reason == REASON_CODER_PROTOCOL_ERROR:
        return ResumeDecision.REUSE_HALT

    if is_no_completion_reason(facts.reason):
        return ResumeDecision.COUNT_NO_COMPLETION_AND_RETRY

    # Defensive fallthrough: should be unreachable given _is_known
    # gate, but keeps the function total without raising.
    return ResumeDecision.INVALID_SUMMARY


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_KNOWN_STATUS_REASON_PAIRS: frozenset[tuple[str, str]] = frozenset({
    ("ok", REASON_REVIEWER_OK),
    ("stopped", REASON_REVIEWER_REPORTS_NO_PROGRESS),
    ("stopped", REASON_MAX_ROUNDS_EXCEEDED),
    ("error", REASON_REVIEWER_NO_COMPLETION),
    ("error", REASON_CODER_NO_COMPLETION),
    ("error", REASON_CODER_PROTOCOL_ERROR),
})


def _is_known(status: Any, reason: Any) -> bool:
    if not isinstance(status, str) or not isinstance(reason, str):
        return False
    return (status, reason) in _KNOWN_STATUS_REASON_PAIRS


def _is_stale(facts: ResumeFacts) -> bool:
    """True when the cache cannot represent the current state.

    Cases:

    - require_validation=True and current head_sha is unknown
      (orchestrator can't prove the current commit was validated).
    - Cached head_sha differs from current head_sha (new commit
      between cache and now).
    - require_validation=True and the cache cannot prove its own
      validation passed (legacy summary with no embedding and no
      sibling validation-record).
    - Current validation explicitly failed (validation flipped on
      the same SHA, e.g. flake fixed itself or env regression —
      cached approval no longer holds).
    """
    if facts.require_validation and facts.current_head_sha is None:
        return True
    # When the current head is known, the cached head_sha must match
    # — including when the cache can't prove a head_sha at all. A
    # cached summary that doesn't know which commit it covers cannot
    # be safely reused for a known current commit. (This rejects the
    # "cache present but pre-PR — no head_sha embedded — coexists
    # with a known current head_sha" case, even when
    # require_validation=False.)
    if (
        facts.current_head_sha is not None
        and facts.cached_head_sha != facts.current_head_sha
    ):
        return True
    if facts.require_validation and not facts.cached_validation_passed:
        # Cache could not prove its commit was validated. Includes
        # the False case (record present, passed: false) and the
        # None case (no record, no embedded head_sha). Both mean
        # "we can't trust this cache under require_validation".
        return True
    if facts.current_validation_failed:
        return True
    return False
