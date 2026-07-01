"""Shared policy: does a completed PR need the configured code review queued?

Both the live completion path (:class:`~.completion_handler.CompletionHandler`)
and the publish-retry reconciliation path
(:class:`~.publish_recovery.PublishRecoveryService`) must answer the same
question when a session produces a PR: "does this PR still need the configured
code review, or is it done?" Keeping that decision in one place stops the retry
path from drifting from live completion behavior — the exact drift behind the F1
bug, where a retry-published PR silently bypassed the review gate.

This owns only the *session-level* distinctions. The planner still owns the
downstream gates it always has (dry-run PRs, and whether a review is already
queued for that PR), so callers route a positive decision through the existing
``DiscoveredReview`` fact rather than re-implementing those gates.
"""

from __future__ import annotations


def should_queue_pr_review(
    *,
    has_pr: bool,
    code_review_agent_configured: bool,
    skip_review: bool,
    is_review_session: bool,
    review_exchange_completed: bool,
    review_exchange_halted: bool,
) -> bool:
    """Return whether a completed PR should be routed into the code-review queue.

    Mirrors the live completion decision exactly: a PR needs review only when a
    review agent is configured, the session did not opt out of review, it is not
    itself a review session, and no local review exchange already
    completed/halted for it.
    """
    if review_exchange_completed or review_exchange_halted:
        return False
    if is_review_session:
        return False
    if skip_review:
        return False
    return has_pr and code_review_agent_configured
