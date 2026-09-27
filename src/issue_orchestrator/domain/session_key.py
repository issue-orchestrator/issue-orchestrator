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
    key = SessionKey(issue=issue_key, kind=SessionKind.CODE)
    if key in active_session_keys:
        # Don't launch another

    # Remove session when done
    active_sessions.pop(key)
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .session_kind import SessionKind

if TYPE_CHECKING:
    from .issue_key import IssueKey


@dataclass(frozen=True)
class SessionKey:
    """Slot identity for a session.

    Identifies "what work, what kind" - not "who" or "how".

    Attributes:
        issue: The work item this session relates to
        kind: The authoritative kind of the session (see ``session_kind``)

    Examples:
        >>> from .issue_key import FakeIssueKey
        >>> key1 = SessionKey(issue=FakeIssueKey("M1-011"), kind=SessionKind.CODE)
        >>> key2 = SessionKey(issue=FakeIssueKey("M1-011"), kind=SessionKind.CODE)
        >>> key1 == key2
        True
        >>> key1.stable_id()
        'code:M1-011'
    """

    issue: "IssueKey"
    kind: SessionKind

    def stable_id(self) -> str:
        """Stable, human-meaningful identifier.

        Format: "{kind}:{issue_stable_id}"
        Examples: "code:M1-011", "review:M1-011"
        """
        return f"{self.kind.value}:{self.issue.stable_id()}"

    def __str__(self) -> str:
        """Human-readable representation including scope."""
        return f"{self.kind.value}:{self.issue}"

    # NOTE: `__hash__`/`__eq__` call `issue.scope()`, which refuses an empty
    # repository (#7255). A key built without one therefore RAISES on a dict
    # lookup or comparison instead of missing or returning False. Every storage
    # boundary refuses such a key on the way in and out, so one cannot reach
    # here in production; a fake IssueKey in a test can.
    def __hash__(self) -> int:
        """Hash based on kind and issue identity."""
        return hash((self.kind, self.issue.stable_id(), self.issue.scope()))

    def __eq__(self, other: object) -> bool:
        """Structural equality based on kind and issue identity."""
        if not isinstance(other, SessionKey):
            return NotImplemented
        return (
            self.kind == other.kind
            and self.issue.stable_id() == other.issue.stable_id()
            and self.issue.scope() == other.issue.scope()
        )
