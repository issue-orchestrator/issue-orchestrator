"""Non-blocking outcomes for issue-wide disposition mutations."""

from enum import Enum


class IssueDispositionGateStatus(Enum):
    ACQUIRED = "acquired"
    BUSY = "busy"
