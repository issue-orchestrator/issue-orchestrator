"""Port for durable, phase-aware stale needs-human clear provenance (#6771 r7/r8).

When a recovered triage investigation supersedes an incomplete needs-human
escalation, the orchestrator owns the removal of the stale source-of-truth
label it applied. That removal is an external mutation and can fail, and the
launch that owes it can itself be lost to a crash the instant after it creates
the terminal — so the OBLIGATION to clear must be durable *before* the terminal
becomes restorable, and it must survive a process restart.

A single flat "issue is owed a clear" record cannot express that safely: writing
a bare clear before launch would strip a legitimate label if the launch then
fails (the investigation never started, so the label is still correct). The
record is therefore phase-aware (:class:`NeedsHumanClearPhase`):

- ``PENDING`` is written BEFORE ``launch_issue_session`` creates the terminal.
  It proves the orchestrator OWNS the eventual clear no matter when a crash
  lands, but is NOT yet eligible for the removal — the launch may still fail.
- ``CONFIRMED`` is written once the launch commits (the terminal exists / the
  investigation is restorable). Only then is the stale label contradictory and
  its removal owed; the removal is retried until it commits, across restarts.

This record is the ONLY proof that the orchestrator itself owns the pending
clear. Restart recovery reads it instead of inferring ownership from "an active
session still carries needs-human" — a conjunction of two independently valid
facts (a running session AND a globally meaningful human-control label) that an
operator or a running session transitioning to needs-human also satisfies, so
inferring ownership from it would strip a legitimate stop/escalation on the next
tick (#6771 round 7 finding). A restart uses the active-session signal ONLY to
decide whether a PENDING launch COMMITTED (confirm) or failed (withdraw) — never
to manufacture ownership, which the durable record already establishes.

Constructed once at the composition root (``entrypoints/bootstrap.py``) and
injected into the session launcher, which owns the needs-human lifecycle. Tests
mock this protocol; the durable JSON implementation lives in
``execution/json_needs_human_clear_store.py``.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol


class NeedsHumanClearPhase(str, Enum):
    """Lifecycle phase of an orchestrator-owed stale needs-human clear.

    An issue is in exactly one phase at a time (the store is keyed by issue).
    """

    #: Written before the terminal exists. Ownership is durable, but the removal
    #: waits: a launch that fails leaves the needs-human label legitimate.
    PENDING = "pending"
    #: The launch committed; the stale label now contradicts running work and
    #: its removal is owed and retried until it commits (round-8 write-ahead).
    CONFIRMED = "confirmed"


class NeedsHumanClearStore(Protocol):
    """Durable, phase-aware provenance of orchestrator-owed needs-human clears."""

    def record_pending(self, issue_number: int) -> None:
        """Persist a PROVISIONAL clear obligation before the terminal exists.

        Idempotent, and never downgrades an already-``CONFIRMED`` obligation.
        """
        ...

    def confirm(self, issue_number: int) -> None:
        """Promote an obligation to ``CONFIRMED`` once the launch commits.

        The stale label is now contradictory, so its removal is owed. Writes
        ``CONFIRMED`` durably before the removal is attempted so a crash mid-clear
        leaves a record the reconciler retries.
        """
        ...

    def withdraw(self, issue_number: int) -> None:
        """Drop an obligation entirely. No-op if absent.

        Used both when the owed removal has committed (satisfied) and when a
        launch never committed (moot — the needs-human label is legitimate and
        must NOT be cleared).
        """
        ...

    def pending_issue_numbers(self) -> list[int]:
        """Issue numbers whose obligation is still ``PENDING`` (unconfirmed)."""
        ...

    def confirmed_issue_numbers(self) -> list[int]:
        """Issue numbers whose ``CONFIRMED`` removal is still owed (uncommitted)."""
        ...


class InMemoryNeedsHumanClearStore:
    """Non-durable, phase-aware store for tests that skip restart durability."""

    def __init__(self) -> None:
        self._phases: dict[int, NeedsHumanClearPhase] = {}

    def record_pending(self, issue_number: int) -> None:
        # setdefault: never downgrade an already-CONFIRMED obligation to PENDING.
        self._phases.setdefault(issue_number, NeedsHumanClearPhase.PENDING)

    def confirm(self, issue_number: int) -> None:
        self._phases[issue_number] = NeedsHumanClearPhase.CONFIRMED

    def withdraw(self, issue_number: int) -> None:
        self._phases.pop(issue_number, None)

    def pending_issue_numbers(self) -> list[int]:
        return [n for n, p in self._phases.items() if p is NeedsHumanClearPhase.PENDING]

    def confirmed_issue_numbers(self) -> list[int]:
        return [n for n, p in self._phases.items() if p is NeedsHumanClearPhase.CONFIRMED]
