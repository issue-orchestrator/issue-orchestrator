"""Ports of the tech-lead improver's run (#7490).

* :class:`ImproverRunReader` — the read port an operator surface and the next
  run use: every recorded run, newest first, and an accepted run's findings.
* :class:`ImproverRunStore` — the run owner's write side.
* :class:`ImproverAgent` — the untrusted agent: given the prompt and a run
  directory it can only read, it returns its final message, never a write.
  Each provider (Claude, Codex) is one adapter; ``choice`` says which, and
  on what model, so the run records it.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..contracts.improver_findings import ImproverFindings
from ..contracts.improver_run import ImproverAgentChoice, ImproverRunRecord


class ImproverRunReader(Protocol):
    def runs(self) -> tuple[ImproverRunRecord, ...]:
        """Every recorded run, newest first."""
        ...

    def accepted_findings(self, run: ImproverRunRecord) -> ImproverFindings:
        """The findings an ACCEPTED run's validation let through."""
        ...


class ImproverStoreBusy(RuntimeError):
    """Another process holds the run store (a run or an apply is in progress)."""


class ImproverRunStore(ImproverRunReader, Protocol):
    def exclusive(self) -> AbstractContextManager[None]:
        """Hold the store for one run or one application of owed effects.

        Raises :class:`ImproverStoreBusy` at once when another process holds
        it: two runs, or a run and an ``apply``, must never apply the same
        owed effects side by side.
        """
        ...

    def new_run_dir(self, run_id: str) -> Path:
        """A fresh, empty working directory for ``run_id``."""
        ...

    def record(self, run: ImproverRunRecord) -> None:
        """Durably replace ``run``'s record."""
        ...


@dataclass(frozen=True)
class ImproverAgentResult:
    """The agent's final message, or why there is none."""

    final_message: str | None
    detail: str


class ImproverAgent(Protocol):
    @property
    def choice(self) -> ImproverAgentChoice:
        """The provider and model this agent runs on."""
        ...

    def run(self, *, prompt: str, run_dir: Path) -> ImproverAgentResult: ...


__all__ = [
    "ImproverAgent",
    "ImproverAgentResult",
    "ImproverRunReader",
    "ImproverRunStore",
    "ImproverStoreBusy",
]
