"""Port: everything the engine adds to one agent's launch prompt (#8141)."""

from __future__ import annotations

from typing import Protocol

from ..domain.launch_prompt import LaunchPromptPreparation
from ..domain.session_kind import SessionKind


class LaunchPromptProvider(Protocol):
    """Resolve the additions for a *kind* session on issue *issue_number*."""

    def prepare(self, *, kind: SessionKind, issue_number: int) -> LaunchPromptPreparation:
        """All I/O happens here, before the caller mutates launch state."""
        ...
