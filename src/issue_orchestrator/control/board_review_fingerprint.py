"""What the health review would walk, reduced to a comparable value.

Single owner for "has the reviewable board changed since we last looked?" — the
question the periodic health-review trigger asks to avoid re-walking a board
nobody has touched (ADR-0031 §4, #6793).

The board is fingerprinted by IDENTITY where a health review reasons about
identity (which issues are blocked, which sessions are running, which failed)
and by DEPTH where it only reasons about volume (the pending queues). So a job
starting, finishing, blocking, or failing flips the fingerprint, while a 1-for-1
queue swap at constant depth does not — normal throughput is not a reason to
re-walk the board.

Consumed by ``health_review_trigger``, which pairs it with the interval floor.
Note the pairing rule that lives there: the fingerprint a decision fires on is
the one that must later be recorded as reviewed. Recomputing it at record time
reads a board that has already changed (the anchor itself is on it by then), and
a value that never matches means a gate that never suppresses.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, Session

# Deliberately a fixed constant rather than a config knob. The adjacent
# ``observability.session_no_output_seconds`` (default 120) is a different
# concept at a different altitude: it throttles a per-session WARNING event, so
# it is tuned for noise. This threshold declares a session wedged for the
# purpose of re-walking the whole board, and sits below the default 45-minute
# ``session_timeout_minutes`` that would otherwise reap the session — it exists
# to buy the review a window BEFORE the timeout fires, which is a property of
# the trigger, not of the deployment.
_HUNG_SESSION_NO_OUTPUT_SECONDS = 30 * 60


def _session_silent_since(session: "Session", now: float) -> float:
    """Seconds since this session last showed a sign of life.

    ``last_output_at`` is set by the observer only once the session's log file
    exists AND changes, so it stays None for a session wedged before its first
    write (agent CLI failed to spawn, log never materialized). Falling back to
    ``started_at`` keeps that case measurable: it is the most severely hung
    session there is, and keying "hung" on a field it never sets would make it
    the one case the flag cannot see (#6793 "aging caveat").
    """
    last_sign_of_life = session.last_output_at
    if last_sign_of_life is None:
        # Naive local datetime; .timestamp() resolves it against the same epoch
        # clock ``now`` comes from.
        last_sign_of_life = session.started_at.timestamp()
    return now - last_sign_of_life


def session_is_hung(session: "Session", now: float) -> bool:
    """A running session that has shown no sign of life for a sustained window.

    Uses time-since-last-output (not raw age): a legitimately long task still
    emits output, whereas a wedged one goes silent. Folding this flag into the
    board fingerprint is what lets a session that goes quiet re-trigger a review
    even when nothing else on the board has changed (ADR-0031 §4 aging concern).
    """
    return _session_silent_since(session, now) >= _HUNG_SESSION_NO_OUTPUT_SECONDS


def board_review_fingerprint(state: "OrchestratorState", now: float) -> str:
    """Stable fingerprint of the reviewable board, or "" when nothing is on it.

    Captures the identities/shape a health review would walk: blocked issues,
    active sessions (each with a hung flag), the depth of every pending queue,
    and this cycle's failures. A fully idle board yields "".

    Sessions are keyed by ``SessionKey.stable_id()`` (the domain identity,
    "code:M1-011"), NOT by issue number: two sessions can share an issue, so an
    issue-number key would hide a coding session being replaced by a review
    session on the same issue.

    Deterministic given (state, now): no clock buckets, so the only thing time
    changes is whether a silent session has crossed the hung threshold.
    """
    blocked = sorted(state.dependency_problems)
    sessions = sorted(
        (session.key.stable_id(), session_is_hung(session, now))
        for session in state.active_sessions
    )
    queue_depths = [
        len(state.pending_reviews),
        len(state.pending_reworks),
        len(state.pending_tech_lead_reviews),
        len(state.pending_validation_retries),
        len(state.pending_cleanups),
        len(state.priority_queue),
    ]
    failures = sorted(state.failed_this_cycle)
    if not (blocked or sessions or any(queue_depths) or failures):
        return ""
    canonical = json.dumps(
        [blocked, sessions, queue_depths, failures], separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
