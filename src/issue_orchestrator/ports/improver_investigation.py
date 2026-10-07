"""How an improver run investigates: the port its run uses (#8001).

A run opens an :class:`ImproverInvestigation` around its agent. A SCRIPTED
investigation gives the agent the staged bundle only; an EMPOWERED one
stages the read-only toolbox beside it, serves it for the agent's run and
tells the agent how to use it (:class:`InvestigationKit`).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..contracts.improver_toolbox import ImproverMode
from ..domain.engine_activity import EngineRef
from .improver_toolbox import ToolboxEndpoint


@dataclass(frozen=True)
class InvestigationKit:
    #: The running toolbox; ``None`` for a scripted run.
    toolbox: ToolboxEndpoint | None
    #: Appended to the improver prompt: how this run may investigate.
    instructions: str


class ImproverInvestigation(Protocol):
    @property
    def mode(self) -> ImproverMode: ...

    def open(self, engine: EngineRef, run_dir: Path) -> AbstractContextManager[InvestigationKit]:
        """Prepare what the agent investigates with, for the duration of its run."""
        ...


__all__ = ["ImproverInvestigation", "InvestigationKit"]
