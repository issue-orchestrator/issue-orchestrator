"""Shared scope checks for PR-driven review workflows."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Protocol

from ..domain.branch_naming import extract_issue_number_from_branch
from ..ports.pull_request_tracker import PRInfo
from .issue_scope import IssueScopeDecision, evaluate_issue_scope

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.issue import Issue

logger = logging.getLogger(__name__)
_CLOSING_ISSUE_RE = re.compile(r"\bCloses\s+#(\d+)\b", re.IGNORECASE)


class ReviewIssueReader(Protocol):
    """Repository read surface needed to scope review PRs to issues."""

    def get_issue(self, issue_number: int) -> "Issue | None": ...


@dataclass(frozen=True)
class ReviewScopeResult:
    """Decision for whether a review PR belongs to the current orchestrator scope."""

    in_scope: bool
    reason: str
    issue_number: int
    pr_number: int
    issue: "Issue | None" = None


def extract_issue_number_from_pr(pr: PRInfo) -> int:
    """Extract the linked issue number from an orchestrator PR."""
    if pr.branch:
        issue_from_branch = extract_issue_number_from_branch(pr.branch)
        if issue_from_branch is not None:
            return issue_from_branch

    return extract_issue_number(pr.body, pr.number)


def extract_issue_number(pr_body: str, fallback: int) -> int:
    """Extract issue number from a PR body using the standard closing reference."""
    match = _CLOSING_ISSUE_RE.search(pr_body)
    return int(match.group(1)) if match else fallback


def pr_fields_reference_issue(
    *,
    branch: str | None,
    title: str,
    body: str,
    issue_numbers: Iterable[int],
) -> bool:
    """Return whether PR fields reference any of the issue numbers."""
    issue_number_set = set(issue_numbers)
    if not issue_number_set:
        return False

    if branch:
        issue_from_branch = extract_issue_number_from_branch(branch)
        if issue_from_branch in issue_number_set:
            return True

    body_issue_numbers = {int(match.group(1)) for match in _CLOSING_ISSUE_RE.finditer(body)}
    if issue_number_set & body_issue_numbers:
        return True

    return any(re.search(rf"#{issue_number}\b", title) for issue_number in issue_number_set)


class ReviewScopeChecker:
    """Apply configured issue filters before queueing or mutating review PRs."""

    def __init__(
        self,
        config: "Config",
        issue_reader: ReviewIssueReader,
        *,
        log_prefix: str,
        require_open_issue: bool = False,
    ):
        """Create a scope checker.

        Scanners keep require_open_issue false to preserve their historical
        behavior. Startup recovery and label reconciliation set it true before
        mutating or recovering stale PRs.
        """
        self.config = config
        self.issue_reader = issue_reader
        self.log_prefix = log_prefix
        self.require_open_issue = require_open_issue

    def check_pr(self, pr: PRInfo) -> ReviewScopeResult:
        """Return whether the PR's linked issue is in scope."""
        issue_number = extract_issue_number_from_pr(pr)
        return self.check_issue_number(issue_number, pr.number)

    def is_pr_in_scope(self, pr: PRInfo) -> bool:
        """Boolean adapter for call sites that only need a predicate."""
        return self.check_pr(pr).in_scope

    def check_issue_number(self, issue_number: int, pr_number: int) -> ReviewScopeResult:
        """Return whether a PR linked to issue_number is in configured scope."""
        if self.config.filtering.issue and issue_number != self.config.filtering.issue:
            self._log_skip(
                pr_number,
                issue_number,
                "outside single-issue scope",
                str(self.config.filtering.issue),
            )
            return ReviewScopeResult(False, "outside_single_issue_scope", issue_number, pr_number)

        filter_label = self.config.filtering.label
        issue_filter = self.config.get_issue_filter()

        if not filter_label and issue_filter.is_empty() and not self.require_open_issue:
            return ReviewScopeResult(True, "ok", issue_number, pr_number)

        issue = self.issue_reader.get_issue(issue_number)
        if issue is None:
            self._log_skip(
                pr_number,
                issue_number,
                "linked issue missing",
                level=logging.INFO,
            )
            return ReviewScopeResult(False, "issue_missing", issue_number, pr_number)

        decision = evaluate_issue_scope(
            self.config,
            issue,
            require_open=self.require_open_issue,
            include_milestone_filter=False,
        )
        if not decision.in_scope:
            self._log_scope_decision(pr_number, issue_number, issue, decision)
            return ReviewScopeResult(False, decision.code, issue_number, pr_number, issue)

        return ReviewScopeResult(True, "ok", issue_number, pr_number, issue)

    def _log_scope_decision(
        self,
        pr_number: int,
        issue_number: int,
        issue: "Issue",
        decision: IssueScopeDecision,
    ) -> None:
        message, detail = self._scope_log_args(issue, decision)
        self._log_skip(pr_number, issue_number, message, detail)

    def _scope_log_args(
        self,
        issue: "Issue",
        decision: IssueScopeDecision,
    ) -> tuple[str, str | None]:
        values = {
            "issue_not_open": ("linked issue is not open", issue.state),
            "missing_filter_label": (
                "missing filter label",
                self.config.filtering.label,
            ),
            "excluded_by_label_filter": ("excluded by label filter", None),
        }
        args = values.get(decision.code)
        if args is None:
            return decision.detail or decision.code, None
        return args

    def _log_skip(
        self,
        pr_number: int,
        issue_number: int,
        reason: str,
        detail: str | None = None,
        level: int = logging.DEBUG,
    ) -> None:
        suffix = f" ({detail})" if detail else ""
        logger.log(
            level,
            "[%s] PR #%d linked to issue #%d skipped: %s%s",
            self.log_prefix,
            pr_number,
            issue_number,
            reason,
            suffix,
        )
