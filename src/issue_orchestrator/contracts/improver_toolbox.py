"""The empowered improver's staged toolbox, as the agent reads it (#8001).

``<run dir>/toolbox/toolbox.json`` says what was staged beside it and why
anything was not, as ``improver-data/inputs.json`` does for the bundle.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict

TOOLBOX_DIRNAME = "toolbox"
TOOLBOX_MANIFEST = "toolbox.json"
TOOLBOX_STATE_DIRNAME = "state"
TOOLBOX_LOGS_DIRNAME = "logs"
TOOLBOX_REPO_DIRNAME = "repo"
#: In the run directory: each toolbox answer the agent was served, by call
#: id, so a design finding can cite it (#8001). Written by the orchestrator.
TOOLBOX_ANSWERS_DIRNAME = "toolbox-answers"


class ImproverMode(StrEnum):
    """How the improver investigates."""

    #: The staged bundle only, read-only file tools (the original improver).
    SCRIPTED = "scripted"
    #: The bundle as a starting map, plus the read-only toolbox: GitHub reads
    #: of the audited repository, SQL over the engine's stores, Git history of
    #: the audited repository. The agent chooses its depth within a budget.
    EMPOWERED = "empowered"


#: The tournament's winning arm was empowered (#8001).
DEFAULT_IMPROVER_MODE = ImproverMode.EMPOWERED


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolboxSource(_Closed):
    #: Relative to ``toolbox/``.
    path: str
    staged: bool
    detail: str


class ToolboxManifest(_Closed):
    audited_repo: str
    #: When the copies and the clone were taken: what the agent reads of the
    #: engine is as of then (GitHub reads are live).
    staged_at: AwareDatetime
    sources: tuple[ToolboxSource, ...]


__all__ = [
    "DEFAULT_IMPROVER_MODE",
    "TOOLBOX_ANSWERS_DIRNAME",
    "TOOLBOX_DIRNAME",
    "TOOLBOX_LOGS_DIRNAME",
    "TOOLBOX_MANIFEST",
    "TOOLBOX_REPO_DIRNAME",
    "TOOLBOX_STATE_DIRNAME",
    "ImproverMode",
    "ToolboxManifest",
    "ToolboxSource",
]
