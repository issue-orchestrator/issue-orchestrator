"""Review and rework launch support helpers."""

import json
import logging
import shutil
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..domain.models import PendingReview
from ..domain.session_run import SessionRunAssets
from ..infra.config import Config
from ..ports import Issue as IssueProtocol, RepositoryHost, RepositoryHostError, ReviewState
from ..ports.pull_request_tracker import PRInfo
from ..ports.worktree_manager import WorktreeInfo
from .review_validity import ReviewValidity, evaluate_review_validity

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


def review_launch_validity(
    *,
    review: PendingReview,
    config: Config,
    repository_host: RepositoryHost,
    label_manager: "LabelManager",
) -> ReviewValidity:
    """Load current review facts and decide whether launch is still valid."""
    current_issue = repository_host.get_issue(review.issue_number)
    if not isinstance(current_issue, IssueProtocol):
        current_issue = None
    current_pr = repository_host.get_pr(review.pr_number)
    if not isinstance(current_pr, PRInfo):
        current_pr = None
    return evaluate_review_validity(
        config=config,
        label_manager=label_manager,
        issue=current_issue,
        pr=current_pr,
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
