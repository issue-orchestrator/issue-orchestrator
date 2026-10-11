"""Ports for an issue's standing rulings (#8141).

Two seams:

* :class:`StandingRulingsIndex` - the engine's LOCAL index of what each
  issue's body block holds, so every prompt and review check reads rulings
  without a GitHub call. It is a cache of GitHub, never a second truth: the
  owner fills it from the issue body (or writes through it after a body
  write), and an issue it holds nothing about is hydrated from GitHub on first
  use.
* :class:`StandingRulings` - the behaviour every reader asks
  (:class:`~..control.standing_rulings.StandingRulingsOwner` implements it):
  the active rulings, the prompt section for a session, and the review rule.
"""

from __future__ import annotations


from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..domain.session_kind import SessionKind
from ..domain.standing_ruling import StandingRuling

if TYPE_CHECKING:
    from .issue import Issue


@dataclass(frozen=True, slots=True)
class SyncedRulings:
    """An issue's rulings as the index last synced them from its body, and when."""

    rulings: tuple[StandingRuling, ...]
    #: ISO-8601 instant of the read (or write) that synced them.
    synced_at: str


class StandingRulingsIndex(Protocol):
    """Durable local mirror of each issue's standing rulings."""

    def load(self, issue_number: int) -> tuple[StandingRuling, ...] | None:
        """The issue's rulings as last synced; None when never synced."""
        ...

    def save(self, issue_number: int, rulings: tuple[StandingRuling, ...]) -> None:
        """Replace the issue's synced rulings (an empty tuple is a synced "none")."""
        ...

    def synced(self) -> dict[int, SyncedRulings]:
        """Every synced issue that has at least one ruling, with its sync time."""
        ...


class StandingRulingsUnavailable(RuntimeError):
    """An issue's rulings could not be read: nothing may act without them."""


class StandingRulings(Protocol):
    """What readers ask about an issue's standing rulings."""

    def active(self, issue_number: int) -> tuple[StandingRuling, ...]:
        """The issue's rulings; raises :class:`StandingRulingsUnavailable`."""
        ...

    def prompt_section(self, issue_number: int, kind: SessionKind) -> str | None:
        """The binding section a *kind* session on the issue is given, or None."""
        ...

    def covered_section(self, covered: Mapping[int, tuple[int, ...]]) -> str | None:
        """The binding section for a tech-lead run over other issues' work (#8347).

        *covered* maps each issue the run covers to its PRs in the run. Each
        issue's rulings are read fresh from its body (a number GitHub has no
        issue for binds nothing); raises :class:`StandingRulingsUnavailable` on
        a damaged block, and the read's own error when a read fails.
        """
        ...


class StandingRulingsBackfill(Protocol):
    """Fill the local index from issue bodies a listing just read (#8347)."""

    def backfill(self, issues: Iterable["Issue"]) -> int:
        """Re-read, fresh, each listed issue whose rulings differ from the index; how many."""
        ...


class NoStandingRulings:
    """For compositions with no repository host (tests, offline tools)."""

    def active(self, issue_number: int) -> tuple[StandingRuling, ...]:
        del issue_number
        return ()

    def prompt_section(self, issue_number: int, kind: SessionKind) -> str | None:
        del issue_number, kind
        return None

    def covered_section(self, covered: Mapping[int, tuple[int, ...]]) -> str | None:
        del covered
        return None

    def backfill(self, issues: Iterable["Issue"]) -> int:
        del issues
        return 0



NO_STANDING_RULINGS = NoStandingRulings()
