"""Configured issue scope decisions for launch and reset paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.issue import Issue


@dataclass(frozen=True)
class IssueScopeDecision:
    """Decision for whether an issue belongs to this engine's issue scope."""

    in_scope: bool
    code: str = "ok"
    detail: str | None = None


def evaluate_issue_scope(
    config: "Config",
    issue: "Issue",
    *,
    require_open: bool = True,
    include_milestone_filter: bool = True,
    include_issue_number_filter: bool = False,
) -> IssueScopeDecision:
    """Apply the configured issue-scope gates to one issue snapshot.

    ``include_issue_number_filter`` is opt-in because queue snapshots retain
    all scoped issues, then apply single-issue scheduling at queue eligibility.
    Hidden reset preflight enables it to explain whether the requested issue
    can be reintroduced by this engine.
    """
    if require_open:
        current = str(issue.state or "").lower()
        if current == "closed":
            return _outside("issue_not_open", "issue is closed")

    marks = list(issue.labels)
    required_mark = config.filtering.label
    if required_mark and required_mark not in marks:
        return _outside(
            "missing_filter_label",
            f'missing required filter label "{required_mark}"',
        )

    if include_milestone_filter:
        allowed_milestones = config.get_filter_milestones()
        current_milestone = issue.milestone
        if allowed_milestones and current_milestone not in allowed_milestones:
            displayed_milestone = current_milestone or "none"
            return _outside(
                "outside_milestone_filter",
                f'milestone "{displayed_milestone}" is not one of '
                f"{', '.join(allowed_milestones)}",
            )

    detail = config.get_issue_filter().exclusion_reason(issue)
    if detail is not None:
        return _outside("excluded_by_label_filter", detail)

    if include_issue_number_filter:
        target_number = config.filtering.issue
        if target_number and issue.number != target_number:
            return _outside(
                "outside_single_issue_scope",
                f"engine is scoped to issue #{target_number}",
            )

    return IssueScopeDecision(in_scope=True)


def issue_scope_skip_detail(
    config: "Config",
    issue: "Issue",
    *,
    require_open: bool = True,
    include_milestone_filter: bool = True,
    include_issue_number_filter: bool = False,
) -> str | None:
    """Return scope skip detail for callers that only need the reason text."""
    decision = evaluate_issue_scope(
        config,
        issue,
        require_open=require_open,
        include_milestone_filter=include_milestone_filter,
        include_issue_number_filter=include_issue_number_filter,
    )
    return decision.detail


def _outside(code: str, detail: str) -> IssueScopeDecision:
    return IssueScopeDecision(in_scope=False, code=code, detail=detail)
