"""Event context for consistent payload enrichment.

This module provides EventContext, which carries run-level and tick-level
context that should be included in all event payloads for correlation
and debugging purposes.
"""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from .catalog import EVENT_SCHEMA_VERSION


@dataclass
class EventContext:
    """Context for enriching event payloads.

    Carries identifiers that should be included in all events for correlation:
    - run_id: UUID for the orchestrator process lifetime
    - tick_id: Monotonically increasing tick counter (for deterministic tests)

    Usage:
        ctx = EventContext()  # Created once at orchestrator startup
        ctx.tick_id = 0

        # In each tick:
        ctx.tick_id += 1
        payload = ctx.enrich({"issue_key": "M1-011"})
        # payload now includes run_id, tick_id, schema
    """

    run_id: UUID = field(default_factory=uuid4)
    tick_id: int = 0

    def enrich(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add context fields to an event payload.

        Args:
            payload: The original event payload dict

        Returns:
            New dict with context fields added (does not mutate original)
        """
        return {
            "schema": EVENT_SCHEMA_VERSION,
            "run_id": str(self.run_id),
            "tick_id": self.tick_id,
            **payload,
        }

    def for_issue(
        self, issue_key: str, issue_number: int | None = None
    ) -> dict[str, Any]:
        """Create a base payload for issue-related events.

        Args:
            issue_key: Stable issue key like "M1-011"
            issue_number: Optional GitHub issue number

        Returns:
            Dict with context fields and issue identifiers
        """
        result = self.enrich({"issue_key": issue_key})
        if issue_number is not None:
            result["issue_number"] = issue_number
        return result

    def for_session(
        self,
        session_id: str,
        issue_key: str,
        issue_number: int | None = None,
    ) -> dict[str, Any]:
        """Create a base payload for session-related events.

        Args:
            session_id: Session identifier
            issue_key: Stable issue key like "M1-011"
            issue_number: Optional GitHub issue number

        Returns:
            Dict with context fields and session/issue identifiers
        """
        result = self.for_issue(issue_key, issue_number)
        result["session_id"] = session_id
        return result
