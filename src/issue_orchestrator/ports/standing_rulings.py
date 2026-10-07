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


from dataclasses import dataclass
from typing import Protocol

from ..domain.session_kind import SessionKind
from ..domain.standing_ruling import StandingRuling


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



class NoStandingRulings:
    """For compositions with no repository host (tests, offline tools)."""

    def active(self, issue_number: int) -> tuple[StandingRuling, ...]:
        del issue_number
        return ()

    def prompt_section(self, issue_number: int, kind: SessionKind) -> str | None:
        del issue_number, kind
        return None



NO_STANDING_RULINGS = NoStandingRulings()
