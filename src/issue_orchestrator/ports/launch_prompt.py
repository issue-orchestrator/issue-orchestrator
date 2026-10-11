"""Port: everything the engine adds to one agent's launch prompt (#8141)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from ..domain.launch_prompt import LaunchPromptPreparation
from ..domain.session_kind import SessionKind


class LaunchPromptProvider(Protocol):
    """Resolve the additions for a *kind* session on issue *issue_number*."""

    def prepare(self, *, kind: SessionKind, issue_number: int) -> LaunchPromptPreparation:
        """All I/O happens here, before the caller mutates launch state."""
        ...

    def covered_rulings(self, covered: Mapping[int, tuple[int, ...]]) -> str | None:
        """The standing rulings binding a tech-lead run over other issues' work
        (#8347): *covered* maps each issue to its PRs in the run. Raises
        :class:`~.standing_rulings.StandingRulingsUnavailable` when one cannot be read."""
        ...
