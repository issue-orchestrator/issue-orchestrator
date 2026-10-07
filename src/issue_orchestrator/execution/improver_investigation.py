"""The improver's two investigation modes (#8001).

* :class:`ScriptedInvestigation` — the staged bundle only.
* :class:`EmpoweredInvestigation` — the bundle as a starting map, plus the
  read-only toolbox: staged beside it (:mod:`.improver_toolbox_staging`),
  served for the agent's run (:mod:`.improver_toolbox`) and explained to the
  agent by the empowered addendum
  (``examples/prompts/tech-lead-improver-empowered.md``), with its budget.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from ..contracts.improver_toolbox import TOOLBOX_DIRNAME, ImproverMode, ToolboxManifest
from ..domain.engine_activity import EngineRef
from ..ports.improver_investigation import InvestigationKit
from ..ports.improver_toolbox import AuditedRepoReads
from .improver_toolbox import ImproverToolbox, serve_toolbox

#: The addendum, relative to the io checkout.
EMPOWERED_ADDENDUM = Path("examples/prompts/tech-lead-improver-empowered.md")
_PLACEHOLDERS = ("<<AUDITED_REPO>>", "<<BUDGET_MINUTES>>", "<<STAGED_AT>>")


class ToolboxStaging(Protocol):
    """Puts the toolbox in a run dir: staged from a live engine
    (:class:`.improver_toolbox_staging.ImproverToolboxStager`) or copied from
    a frozen snapshot (a tournament's arms)."""

    def stage(self, engine: EngineRef, run_dir: Path) -> ToolboxManifest: ...


class ScriptedInvestigation:
    mode = ImproverMode.SCRIPTED

    @contextmanager
    def open(self, engine: EngineRef, run_dir: Path, *, hidden_issues: frozenset[int]) -> Iterator[InvestigationKit]:
        yield InvestigationKit(toolbox=None, instructions="")


class EmpoweredInvestigation:
    mode = ImproverMode.EMPOWERED

    def __init__(
        self,
        *,
        stager: ToolboxStaging,
        github: Callable[[str], AuditedRepoReads | None],
        addendum: str,
        budget_minutes: int,
    ) -> None:
        if budget_minutes <= 0:
            raise ValueError("an empowered run needs a positive budget")
        missing = [p for p in _PLACEHOLDERS if p not in addendum]
        if missing:
            raise ValueError(f"the empowered addendum lacks {', '.join(missing)}")
        self._stager = stager
        self._github = github
        self._addendum = addendum
        self._budget = budget_minutes

    @contextmanager
    def open(self, engine: EngineRef, run_dir: Path, *, hidden_issues: frozenset[int]) -> Iterator[InvestigationKit]:
        manifest = self._stager.stage(engine, run_dir)
        toolbox = ImproverToolbox(
            run_dir=run_dir,
            audited_repo=engine.repo,
            github=self._github(engine.repo),
            hidden_issues=hidden_issues,
        )
        instructions = (
            self._addendum.replace("<<AUDITED_REPO>>", engine.repo)
            .replace("<<BUDGET_MINUTES>>", str(self._budget))
            .replace("<<STAGED_AT>>", manifest.staged_at.isoformat())
        )
        with serve_toolbox(toolbox) as endpoint:
            yield InvestigationKit(
                toolbox=endpoint, instructions=instructions, evidence=((run_dir / TOOLBOX_DIRNAME).resolve(),)
            )


__all__ = ["EMPOWERED_ADDENDUM", "EmpoweredInvestigation", "ScriptedInvestigation", "ToolboxStaging"]
