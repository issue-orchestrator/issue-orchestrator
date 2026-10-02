"""Review and rework launch support helpers."""

import json
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..domain.models import PendingReview
from ..domain.session_run import SessionRunAssets
from ..infra.config import Config
from ..ports import Issue as IssueProtocol, RepositoryHost, RepositoryHostError, ReviewState
from ..ports.pull_request_tracker import PRInfo
from ..ports.worktree_manager import WorktreeInfo
from ..events import EventName
from ..ports import EventSink, make_trace_event
from .session_launch_types import REVIEW_HELD_BY_RECOVERY, LaunchDisposition, LaunchResult
from .transition_log import log_transition
from .recovery_review_hold import RecoveryHolds
from .review_validity import (
    ReviewValidity,
    evaluate_review_validity,
    evaluate_review_withholding,
)
from .review_question_hold import ReviewQuestionHolds

if TYPE_CHECKING:
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)


def build_review_existing_work(
    *,
    worktree_info: WorktreeInfo,
    pr_number: int,
    repository_host: RepositoryHost,
    keep_current_label: str,
) -> str | None:
    """Build review prompt context from branch state and PR labels."""
    existing_work: str | None = None
    if worktree_info.rebase_failed:
        existing_work = (
            "WARNING: This PR branch could not be rebased onto main due to merge conflicts. "
            "The branch is behind main. When reviewing, consider whether merge conflicts "
            "need to be resolved before the PR can be merged."
        )
        logger.warning("[launch] Rebase failed for review - PR branch is behind main")

    pr_info = repository_host.get_pr(pr_number)
    if not pr_info:
        return existing_work

    if keep_current_label not in pr_info.labels:
        return existing_work

    keep_current_note = (
        f"REVIEWER INSTRUCTION: This PR is labeled '{keep_current_label}'. "
        "Keep the current approach. Do not propose alternative approaches unless "
        "the current approach cannot work or violates correctness, safety, or security. "
        "If the current approach is invalid, fail the review with a brief note."
    )
    if existing_work:
        return f"{existing_work}\n\n{keep_current_note}"
    return keep_current_note


@dataclass(frozen=True)
class ReviewLaunchCheck:
    """A queued review's launch-time facts, as the review owners judge them.

    ``held_by_recovery`` is true exactly when the validated-work recovery
    hold (``recovery-pending``) is ALL that keeps the review from launching
    AND the recovery owner confirms it holds the issue (#7455): that owner
    routes the published work to review and then releases the hold, so the
    review must wait for it. A lingering label the owner does not hold is a
    block like any other and withdraws the review. ``hold_unreadable`` names an
    owner read that failed: the launch fails and is retried, never waited on.
    """

    validity: ReviewValidity
    held_by_recovery: bool
    hold_unreadable: str | None = None


def review_launch_validity(
    *,
    review: PendingReview,
    config: Config,
    repository_host: RepositoryHost,
    label_manager: "LabelManager",
    recovery_holds: RecoveryHolds,
    question_holds: ReviewQuestionHolds,
) -> ReviewLaunchCheck:
    """Load current review facts and decide whether launch is still valid."""
    current_issue = repository_host.get_issue(review.issue_number)
    if not isinstance(current_issue, IssueProtocol):
        current_issue = None
    current_pr = repository_host.get_pr(review.pr_number)
    if not isinstance(current_pr, PRInfo):
        current_pr = None
    admitted = question_holds.review_admitted_blocks(current_issue)
    if current_issue is None or current_pr is None:
        validity = evaluate_review_validity(
            config=config,
            label_manager=label_manager,
            issue=current_issue,
            pr=current_pr,
            review_admitted_blocks=admitted,
        )
        return ReviewLaunchCheck(validity, held_by_recovery=False)
    withholding = evaluate_review_withholding(
        config=config,
        label_manager=label_manager,
        issue=current_issue,
        pr=current_pr,
        block_label=label_manager.recovery_pending,
        review_admitted_blocks=admitted,
    )
    if not withholding.withheld_only_by_block:
        return ReviewLaunchCheck(withholding.current, held_by_recovery=False)
    try:
        held = recovery_holds.holds_recovery(review.issue_number)
    except Exception as error:  # store-defined read failure
        return ReviewLaunchCheck(
            withholding.current,
            held_by_recovery=False,
            hold_unreadable=f"{type(error).__name__}: {error}",
        )
    return ReviewLaunchCheck(withholding.current, held_by_recovery=held)


def refuse_unlaunchable_review(
    check: ReviewLaunchCheck, review: PendingReview, events: EventSink
) -> LaunchResult | None:
    """The launch result for a queued review its live facts no longer admit.

    ``None`` when the review may launch. A review held ONLY by the recovery
    owner waits on its queue (``HELD_BY_RECOVERY``); any other invalid review
    is withdrawn (``WITHDRAWN``) - dropped, and reported as a withdrawal, not
    a failed launch (#7455).
    """
    validity = check.validity
    if check.hold_unreadable is not None:
        return LaunchResult(
            None,
            False,
            f"Could not read the recovery hold of issue #{review.issue_number}: {check.hold_unreadable}",
            disposition=LaunchDisposition.RETRYABLE_FAILURE,
        )
    if check.held_by_recovery:
        # The recovery owner routes this published work and then releases
        # its hold: wait for it, keep the review queued.
        log_transition("review", review.pr_number, "QUEUED", "QUEUED", "held by recovery-pending")
        logger.info(
            "[launch] Review waits for recovery release: pr=%s issue=%s",
            review.pr_number,
            review.issue_number,
        )
        _publish_review_skipped(events, review, REVIEW_HELD_BY_RECOVERY)
        return LaunchResult(
            None,
            False,
            "Review waits for its issue's recovery hold to be released",
            disposition=LaunchDisposition.HELD_BY_RECOVERY,
        )
    if validity.valid:
        return None
    log_transition(
        "review", review.pr_number, "QUEUED", "SKIP", f"stale pending review: {validity.reason}"
    )
    logger.info(
        "[launch] Dropping stale pending review: pr=%s issue=%s reason=%s issue_labels=%s pr_labels=%s",
        review.pr_number,
        review.issue_number,
        validity.reason,
        ",".join(validity.issue_labels) or "(missing)",
        ",".join(validity.pr_labels) or "(none)",
    )
    _publish_review_skipped(events, review, f"stale_pending_review:{validity.reason}")
    return LaunchResult(
        None,
        False,
        f"Stale pending review: {validity.reason}",
        disposition=LaunchDisposition.WITHDRAWN,
    )


def _publish_review_skipped(events: EventSink, review: PendingReview, reason: str) -> None:
    events.publish(
        make_trace_event(
            EventName.REVIEW_SKIPPED,
            {
                "pr_number": review.pr_number,
                "issue_number": review.issue_number,
                "reason": reason,
            },
        )
    )


def find_review_feedback_file(
    worktree_path: Path,
    pr_number: int,
) -> Path | None:
    """Find reviewer feedback from the most recent review session."""
    sessions_dir = worktree_path / ".issue-orchestrator" / "sessions"
    if not sessions_dir.exists():
        return None

    review_suffix = f"__review-{pr_number}"
    review_dirs = sorted(
        [d for d in sessions_dir.iterdir() if d.is_dir() and d.name.endswith(review_suffix)],
        key=lambda d: d.name,
        reverse=True,
    )

    for review_dir in review_dirs:
        feedback_file = review_dir / "reviewer-feedback.json"
        if feedback_file.exists():
            return feedback_file

    return None


def copy_review_feedback_to_rework(
    *,
    worktree_path: Path,
    pr_number: int,
    rework_run_assets: SessionRunAssets,
) -> Path | None:
    """Copy reviewer feedback from the latest review run into a rework run."""
    source_file = find_review_feedback_file(worktree_path, pr_number)
    if not source_file:
        logger.debug(
            "[launch] No review feedback file found for PR #%s in worktree %s",
            pr_number,
            worktree_path,
        )
        return None

    dest_file = rework_run_assets.run_dir / "reviewer-feedback.json"
    try:
        shutil.copy2(source_file, dest_file)
        logger.info(
            "[launch] Copied reviewer feedback for PR #%s: %s -> %s",
            pr_number,
            source_file,
            dest_file,
        )
        return dest_file
    except Exception as e:
        logger.warning(
            "[launch] Failed to copy reviewer feedback for PR #%s: %s",
            pr_number,
            e,
        )
        return None


def read_local_reviewer_feedback(
    *,
    run_dir: Path,
    cache_minutes: int,
) -> str | None:
    """Read local reviewer feedback if the cache entry is still fresh."""
    feedback_file = run_dir / "reviewer-feedback.json"
    if not feedback_file.exists():
        return None

    try:
        data = json.loads(feedback_file.read_text())
        timestamp_str = data.get("timestamp")
        review_issues = data.get("review_issues")

        if not timestamp_str or not review_issues:
            return None

        if cache_minutes < 0:
            return None

        feedback_time = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        age_minutes = (datetime.now(timezone.utc) - feedback_time).total_seconds() / 60

        if age_minutes <= cache_minutes:
            logger.info(
                "[launch] Using local reviewer feedback (age: %.1f min, cache window: %d min)",
                age_minutes,
                cache_minutes,
            )
            return review_issues

        logger.debug(
            "[launch] Local feedback too old (age: %.1f min, cache window: %d min), will fetch from GitHub",
            age_minutes,
            cache_minutes,
        )
        return None

    except Exception as e:
        logger.warning("[launch] Failed to read local reviewer feedback: %s", e)
        return None


def _read_pr_reviews_for_feedback(
    repository_host: RepositoryHost,
    pr_number: int,
) -> list[dict[str, Any]] | None:
    try:
        return repository_host.get_pr_reviews(pr_number)
    except RepositoryHostError:
        raise
    except Exception as e:
        logger.warning("Failed to fetch PR reviews for PR #%s: %s", pr_number, e)
        return None


def format_reviewer_feedback(
    *,
    pr_number: int,
    repository_host: RepositoryHost,
    cache_minutes: int,
    run_assets: SessionRunAssets,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str | None:
    """Extract and format actionable reviewer feedback for a rework prompt."""
    local_feedback = read_local_reviewer_feedback(
        run_dir=run_assets.run_dir,
        cache_minutes=cache_minutes,
    )
    if local_feedback:
        return f"REVIEWER FEEDBACK (address these issues):\n\n{local_feedback}"

    backoff_delays = [1.0, 2.0, 4.0]
    feedback_reviews = []

    for attempt, delay in enumerate(backoff_delays):
        reviews = _read_pr_reviews_for_feedback(repository_host, pr_number)
        if reviews is None:
            return None

        feedback_reviews = [
            r for r in reviews
            if r.get("state") in (ReviewState.CHANGES_REQUESTED.value, ReviewState.COMMENTED.value)
            and r.get("body", "").strip()
        ]

        if feedback_reviews:
            if attempt > 0:
                logger.info(
                    "[launch] Found reviewer feedback after %d retry attempt(s) for PR #%s",
                    attempt,
                    pr_number,
                )
            break

        if attempt < len(backoff_delays) - 1:
            logger.debug(
                "[launch] No reviewer feedback found for PR #%s, retrying in %.1fs (attempt %d/%d)",
                pr_number,
                delay,
                attempt + 1,
                len(backoff_delays),
            )
            sleep_fn(delay)

    if not feedback_reviews:
        logger.info(
            "[launch] No reviewer feedback found for PR #%s after %d attempts",
            pr_number,
            len(backoff_delays),
        )
        return None

    lines = ["REVIEWER FEEDBACK (address these issues):"]
    for review in feedback_reviews:
        reviewer = review.get("user", {}).get("login", "reviewer")
        state = review.get("state", "")
        body = review.get("body", "").strip()
        lines.append(f"\n[{reviewer} - {state}]")
        lines.append(body)

    return "\n".join(lines)


def combine_rework_feedback(*sections: str | None) -> str:
    """Preserve review and scoped instructions, without repeating cached copies."""
    return "\n\n".join(dict.fromkeys(section for section in sections if section))
