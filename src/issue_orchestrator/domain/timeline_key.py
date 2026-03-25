"""Timeline identity for keying timeline streams.

TimelineKey is the domain-level identity for anything that has a timeline.
Today that means GitHub issues and E2E test runs; the design accommodates
future namespaces without changing the storage layer.

The storage layer (SqliteTimelineStore) uses an integer key. TimelineKey
encodes/decodes to that integer transparently:
  - Issues:    positive integers  (issue 123 → store key 123)
  - E2E runs:  negative integers  (run 42   → store key -42)
"""

from __future__ import annotations

from dataclasses import dataclass

_ISSUE_NS = "issue"
_E2E_RUN_NS = "e2e-run"


@dataclass(frozen=True)
class TimelineKey:
    """Stable identity for a timeline stream."""

    namespace: str
    local_id: int

    # -- Factory methods ---------------------------------------------------

    @classmethod
    def for_issue(cls, issue_number: int) -> TimelineKey:
        if issue_number <= 0:
            raise ValueError(f"issue_number must be positive, got {issue_number}")
        return cls(namespace=_ISSUE_NS, local_id=issue_number)

    @classmethod
    def for_e2e_run(cls, run_id: int) -> TimelineKey:
        if run_id <= 0:
            raise ValueError(f"run_id must be positive, got {run_id}")
        return cls(namespace=_E2E_RUN_NS, local_id=run_id)

    # -- Store encoding ----------------------------------------------------

    def to_store_key(self) -> int:
        """Encode as the integer key used by TimelineStore."""
        if self.namespace == _ISSUE_NS:
            return self.local_id
        if self.namespace == _E2E_RUN_NS:
            return -self.local_id
        raise ValueError(f"Unknown timeline namespace: {self.namespace!r}")

    @classmethod
    def from_store_key(cls, key: int) -> TimelineKey:
        """Decode an integer store key back to a TimelineKey."""
        if key > 0:
            return cls(namespace=_ISSUE_NS, local_id=key)
        if key < 0:
            return cls(namespace=_E2E_RUN_NS, local_id=-key)
        raise ValueError("Store key 0 is reserved / invalid")

    # -- Display -----------------------------------------------------------

    @property
    def is_issue(self) -> bool:
        return self.namespace == _ISSUE_NS

    @property
    def is_e2e_run(self) -> bool:
        return self.namespace == _E2E_RUN_NS

    def stable_id(self) -> str:
        return f"{self.namespace}:{self.local_id}"

    def __str__(self) -> str:
        return self.stable_id()
