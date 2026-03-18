"""Runtime-local completion models for synced CLI tools.

These definitions are intentionally self-contained so foreign-repo worktrees
can run `coding-done` / `reviewer-done` without syncing the full domain layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


COMPLETION_RECORD_PATH = ".issue-orchestrator/completion.json"


class CompletionOutcome(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"
    REVIEW_APPROVED = "review_approved"
    REVIEW_CHANGES_REQUESTED = "review_changes_requested"


class RequestedAction(str, Enum):
    PUSH_BRANCH = "push_branch"
    CREATE_PR = "create_pr"
    POST_COMMENT = "post_comment"
    ADD_BLOCKED_LABEL = "add_blocked_label"
    ADD_NEEDS_HUMAN_LABEL = "add_needs_human_label"
    ADD_CODE_REVIEWED_LABEL = "add_code_reviewed_label"
    ADD_NEEDS_REWORK_LABEL = "add_needs_rework_label"
    REMOVE_NEEDS_REWORK_LABEL = "remove_needs_rework_label"
    REMOVE_CODE_REVIEW_LABEL = "remove_code_review_label"


@dataclass(frozen=True)
class ProposedFollowUpIssue:
    """Structured proposal for ancillary work discovered during coding."""

    title: str
    reason: str
    evidence: str | None = None
    suggested_labels: list[str] | None = None
    blocking: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": self.title,
            "reason": self.reason,
            "blocking": self.blocking,
        }
        if self.evidence is not None:
            payload["evidence"] = self.evidence
        if self.suggested_labels is not None:
            payload["suggested_labels"] = self.suggested_labels
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProposedFollowUpIssue":
        title = data.get("title")
        reason = data.get("reason")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("follow_up_issues entries require non-empty title")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("follow_up_issues entries require non-empty reason")
        evidence = data.get("evidence")
        suggested_labels_raw = data.get("suggested_labels")
        suggested_labels = None
        if suggested_labels_raw is not None:
            if not isinstance(suggested_labels_raw, list) or not all(
                isinstance(item, str) and item.strip() for item in suggested_labels_raw
            ):
                raise ValueError("follow_up_issues suggested_labels must be a list of non-empty strings")
            suggested_labels = list(suggested_labels_raw)
        blocking = data.get("blocking", False)
        if not isinstance(blocking, bool):
            raise ValueError("follow_up_issues blocking must be a boolean")
        if evidence is not None and (not isinstance(evidence, str) or not evidence.strip()):
            raise ValueError("follow_up_issues evidence must be a non-empty string when provided")
        return cls(
            title=title,
            reason=reason,
            evidence=evidence if isinstance(evidence, str) else None,
            suggested_labels=suggested_labels,
            blocking=blocking,
        )


@dataclass
class CompletionRecord:
    """Structured completion record written by coding-done/reviewer-done."""

    session_id: str
    timestamp: str
    outcome: CompletionOutcome
    summary: str
    requested_actions: list[RequestedAction] = field(default_factory=list)
    implementation: str | None = None
    problems: str | None = None
    blocked_reason: str | None = None
    blocked_by: list[int] | None = None
    attempted: str | None = None
    when_unblocked: str | None = None
    question: str | None = None
    context: str | None = None
    options: list[str] | None = None
    default_action: str | None = None
    review_summary: str | None = None
    review_issues: str | None = None
    risk_level: str | None = None
    checks_passed: list[str] | None = None
    checks_needed: list[str] | None = None
    comment_body: str | None = None
    pr_labels: list[str] | None = None
    validation_record_path: str | None = None
    follow_up_issues: list[ProposedFollowUpIssue] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "outcome": self.outcome.value,
            "summary": self.summary,
            "requested_actions": [a.value for a in self.requested_actions],
            "implementation": self.implementation,
            "problems": self.problems,
            "blocked_reason": self.blocked_reason,
            "blocked_by": self.blocked_by,
            "attempted": self.attempted,
            "when_unblocked": self.when_unblocked,
            "question": self.question,
            "context": self.context,
            "options": self.options,
            "default_action": self.default_action,
            "review_summary": self.review_summary,
            "review_issues": self.review_issues,
            "risk_level": self.risk_level,
            "checks_passed": self.checks_passed,
            "checks_needed": self.checks_needed,
            "comment_body": self.comment_body,
            "pr_labels": self.pr_labels,
            "validation_record_path": self.validation_record_path,
            "follow_up_issues": [
                issue.to_dict() for issue in self.follow_up_issues
            ] if self.follow_up_issues else None,
        }
