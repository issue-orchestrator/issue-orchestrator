"""Shared E2E issue-creation helpers for Control Center routes."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def create_e2e_sub_issues(
    tracker: Any,
    parent_issue: Any,
    nodeids: list[str],
    results_by_nodeid: dict[str, Any],
    run: Any,
    db: Any,
    run_id: int,
    agent: str,
) -> list[dict[str, Any]]:
    """Create and record sub-issues for selected failing tests."""
    sub_issues: list[dict[str, Any]] = []
    sub_labels = ["e2e:test-failure", agent]

    for nodeid in nodeids:
        test_result = results_by_nodeid.get(nodeid)
        if not test_result:
            logger.warning("[e2e-create-issues] Node ID not found: %s", nodeid)
            continue

        sub_issue = tracker.create_test_failure_issue(
            parent_issue=parent_issue,
            test_result=test_result,
            first_failing_sha=run.commit_sha or "",
            last_passing_sha=None,
            labels=sub_labels,
        )
        if not sub_issue:
            continue

        db.record_failure_issue(
            nodeid=nodeid,
            github_issue_number=sub_issue.issue_number,
            parent_issue_number=parent_issue.issue_number,
            first_failing_run_id=run_id,
            first_failing_sha=run.commit_sha or "",
        )
        sub_issues.append(
            {
                "number": sub_issue.issue_number,
                "url": sub_issue.html_url,
                "nodeid": nodeid,
            },
        )

    return sub_issues


__all__ = ["create_e2e_sub_issues"]
