"""Test doubles for an issue's standing rulings (#8141).

The owner under test is always the REAL :class:`StandingRulingsOwner`; only
its two edges are faked: GitHub's issue bodies (:class:`IssueBodies`) and the
local index (:class:`InMemoryStandingRulingsIndex`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

from issue_orchestrator.control.standing_rulings import StandingRulingsOwner
from issue_orchestrator.ports.standing_rulings import SyncedRulings
from issue_orchestrator.domain.standing_ruling import (
    RulingAuthority,
    RulingScope,
    StandingRuling,
    with_rulings_block,
)

FIXED_NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


@dataclass
class InMemoryStandingRulingsIndex:
    rows: dict[int, tuple[StandingRuling, ...]] = field(default_factory=dict)
    upholds: dict[int, set[str]] = field(default_factory=dict)

    def load(self, issue_number: int) -> tuple[StandingRuling, ...] | None:
        return self.rows.get(issue_number)

    def save(self, issue_number: int, rulings: tuple[StandingRuling, ...]) -> None:
        self.rows[issue_number] = rulings

    def note_upheld(self, issue_number: int, attestation_keys: Iterable[str]) -> None:
        self.upholds.setdefault(issue_number, set()).update(attestation_keys)

    def upheld(self, issue_number: int) -> frozenset[str]:
        return frozenset(self.upholds.get(issue_number, set()))

    def synced(self) -> dict[int, SyncedRulings]:
        return {number: SyncedRulings(rulings, FIXED_NOW.isoformat()) for number, rulings in self.rows.items()
                if rulings}


@dataclass
class IssueBodies:
    """GitHub's issue bodies: reads, verified writes, and how many of each."""

    bodies: dict[int, str] = field(default_factory=dict)
    reads: list[int] = field(default_factory=list)
    writes: list[tuple[int, str]] = field(default_factory=list)
    unreadable: set[int] = field(default_factory=set)

    def read(self, number: int) -> SimpleNamespace | None:
        self.reads.append(number)
        if number in self.unreadable:
            return None
        return SimpleNamespace(number=number, body=self.bodies.get(number, ""), state="open")

    def write(self, number: int, body: str) -> None:
        self.writes.append((number, body))
        self.bodies[number] = body


def rulings_owner(
    bodies: IssueBodies | None = None, index: InMemoryStandingRulingsIndex | None = None
) -> StandingRulingsOwner:
    """The real owner over faked GitHub bodies and an in-memory index."""
    github = bodies if bodies is not None else IssueBodies()
    return StandingRulingsOwner(
        read_issue=github.read, write_body=github.write,
        index=index if index is not None else InMemoryStandingRulingsIndex(), clock=lambda: FIXED_NOW,
    )


def a_ruling(
    ruling_id: str = "m-0123456789ab",
    text: str = "Runtime stamping replaces the static symbol-walk checker.",
    *,
    files: tuple[str, ...] = (),
    claims: tuple[str, ...] = (),
    authority: RulingAuthority = RulingAuthority.MAINTAINER,
) -> StandingRuling:
    return StandingRuling(
        ruling_id=ruling_id, text=text, authority=authority, source="maintainer, in a test",
        scope=RulingScope(files=files, claims=claims), recorded_at=FIXED_NOW.isoformat(),
    )


def body_with(*rulings: StandingRuling, rest: str = "## Outcome\n\nThe issue's own spec.") -> str:
    """An issue body carrying *rulings* in its block, above *rest*."""
    return with_rulings_block(rest, rulings)
