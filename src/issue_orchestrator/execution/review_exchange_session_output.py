"""Filesystem helpers for review-exchange session artifacts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..domain.review_exchange_run import ReviewExchangeRun, ReviewExchangeRunAssets
from ..infra.logging_config import get_repo_log_path
from ..ports.session_output import ReviewExchangeSummary, SessionRunAssets


def start_review_exchange_run(
    start_run: Callable[..., SessionRunAssets],
    worktree_path: Path,
    *,
    issue_number: int,
    parent_session_name: str,
    agent_label: str,
) -> ReviewExchangeRun:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    session_name = f"review-exchange-{issue_number}-{timestamp}"
    run = start_run(
        worktree_path,
        session_name,
        issue_number=issue_number,
        agent_label=agent_label,
        backend="persistent-pty",
        orchestrator_log=str(get_repo_log_path(worktree_path)),
    )
    assets = ReviewExchangeRunAssets.from_run_dir(run.run_dir)
    assets.exchange_dir.mkdir(parents=True, exist_ok=True)
    return ReviewExchangeRun(
        session_name=session_name,
        run_id=run.run_id,
        parent_session_name=parent_session_name,
        assets=assets,
    )


def store_review_exchange_summary(
    review_run: ReviewExchangeRun,
    summary: dict[str, Any],
    *,
    write_json: Callable[[Path, dict[str, Any]], None],
    update_manifest: Callable[[Path, dict[str, str]], None],
    append_run_log_line: Callable[[Path, str], None],
) -> ReviewExchangeSummary:
    run_dir = review_run.assets.run_dir
    exchange_dir = review_run.assets.exchange_dir
    exchange_dir.mkdir(parents=True, exist_ok=True)
    summary_path = review_run.assets.summary_path
    write_json(summary_path, summary)

    updates = {
        "review_exchange_dir": str(exchange_dir),
        "review_exchange_summary_path": str(summary_path),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "outcome": str(summary.get("status") or "completed"),
    }
    if review_run.assets.validation_record_path.exists():
        updates["validation_record_path"] = str(review_run.assets.validation_record_path)
    update_manifest(run_dir, updates)
    append_run_log_line(
        run_dir,
        f"review-exchange status={summary.get('status', 'unknown')} "
        f"reason={summary.get('reason', '')}".strip(),
    )

    return ReviewExchangeSummary(
        summary=summary,
        run_assets=review_run.assets,
    )
