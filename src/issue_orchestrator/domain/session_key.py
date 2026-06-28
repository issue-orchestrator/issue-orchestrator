"""SessionKey - stable slot identity for agent sessions.

A SessionKey answers exactly one question:
"How do I refer to a session slot in a way that prevents duplicates?"

This is NOT about:
- Which agent is running (configuration, can change)
- The terminal name (adapter concern)
- The PR number (derived artifact)
- Historical tracking (use SessionRunId for that)

SessionKey is an identity for a "slot" - ephemeral and reusable after the session ends.
Two keys with same issue + task refer to the same slot.

Usage:
    # Check if slot is occupied
    key = SessionKey(issue=issue_key, task=TaskKind.CODE)
    if key in active_session_keys:
        # Don't launch another

    # Remove session when done
    active_sessions.pop(key)
"""

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .issue_key import IssueKey


class TaskKind(Enum):
    """The kind of task a session is performing.

    This is domain vocabulary, not tied to any external system.
    """

    CODE = "code"        # Working on an issue (writing code)
    REVIEW = "review"    # Reviewing a PR
    RETROSPECTIVE_REVIEW = "retrospective-review"  # Reviewing existing merged work
    REWORK = "rework"    # Fixing issues found in review
    TRIAGE = "triage"    # Triaging failed reviews

    @property
    def is_review_only(self) -> bool:
        """Whether this task is read-only: it makes no commits and publishes nothing.

        Review-only sessions (auditing a PR or existing merged work) produce no
        branch commits, so the publish/code-validation-retry machinery — which
        exists to validate a coder's changes before opening a PR — does not apply
        to them. Treating a review-only session as ordinary coding work leads to
        empty-branch ``create_pr`` attempts (see issue #6426).
        """
        return self in {TaskKind.REVIEW, TaskKind.RETROSPECTIVE_REVIEW}


@dataclass(frozen=True)
class SessionKey:
    """Slot identity for a session.

    Identifies "what work, what task" - not "who" or "how".

    Attributes:
        issue: The work item this session relates to
        task: The kind of task being performed

    Examples:
        >>> from .issue_key import FakeIssueKey
        >>> key1 = SessionKey(issue=FakeIssueKey("M1-011"), task=TaskKind.CODE)
        >>> key2 = SessionKey(issue=FakeIssueKey("M1-011"), task=TaskKind.CODE)
        >>> key1 == key2
        True
        >>> key1.stable_id()
        'code:M1-011'
    """

    issue: "IssueKey"
    task: TaskKind

    def stable_id(self) -> str:
        """Stable, human-meaningful identifier.

        Format: "{task}:{issue_stable_id}"
        Examples: "code:M1-011", "review:M1-011"
        """
        return f"{self.task.value}:{self.issue.stable_id()}"

    def __str__(self) -> str:
        """Human-readable representation including scope."""
        return f"{self.task.value}:{self.issue}"

    def __hash__(self) -> int:
        """Hash based on task and issue identity."""
        return hash((self.task, self.issue.stable_id(), self.issue.scope()))

    def __eq__(self, other: object) -> bool:
        """Structural equality based on task and issue identity."""
        if not isinstance(other, SessionKey):
            return NotImplemented
        return (
            self.task == other.task
            and self.issue.stable_id() == other.issue.stable_id()
            and self.issue.scope() == other.issue.scope()
        )
