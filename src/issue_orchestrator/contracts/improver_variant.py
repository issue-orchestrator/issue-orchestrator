"""The improver's champion, its challengers and their trials (#8001).

The champion is the improver configuration that runs: a prompt, an agent,
a mode, a heat count and a budget. About one run in ten is invited to
propose ONE change to it (:class:`~.improver_findings.ImproverChange`); the
change, applied to the champion, is a challenger. A challenger is tried
against the champion on frozen snapshots through the tournament harness,
and replaces it only when it is told apart above the champion on every
snapshot AND a maintainer approved its issue (#7906's positive ``approved``).
Nothing here touches the answer keys, the graders or the scoring.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .improver_findings import ImproverChange
from .improver_run import ImproverAgentChoice
from .improver_toolbox import ImproverMode
from .improver_tournament import ArmComparison, Slug


class _Closed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ImproverVariant(_Closed):
    """One improver configuration. Its prompt's text is stored by digest."""

    agent: ImproverAgentChoice
    mode: ImproverMode
    heats: Annotated[int, Field(ge=1, le=5)]
    budget_minutes: Annotated[int, Field(ge=10, le=240)]
    prompt_sha256: Sha256

    @property
    def id(self) -> str:
        """A stable name for this exact configuration."""
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()[:12]

    def describe(self) -> str:
        return (
            f"{self.agent.describe()} {self.mode.value}, {self.heats} heat(s), {self.budget_minutes} min,"
            f" prompt {self.prompt_sha256[:10]}"
        )


class Promotion(_Closed):
    """A champion replaced: by which challenge, approved by whom."""

    at: AwareDatetime
    previous: ImproverVariant
    champion: ImproverVariant
    challenge_id: Slug
    issue: str
    approved_by: str


class ChampionState(_Closed):
    """The champion and how it came to be."""

    champion: ImproverVariant
    seeded_at: AwareDatetime
    seeded_by: str
    promotions: tuple[Promotion, ...] = ()


class SnapshotTrial(_Closed):
    """Champion against challenger on one frozen snapshot."""

    snapshot_id: Slug
    tournament_id: Slug
    #: The challenger-versus-champion comparison, as the tournament decided it.
    comparison: ArmComparison
    #: Told apart, with the challenger above.
    challenger_won: bool


class ChallengeRecord(_Closed):
    """A challenger's trial against the champion it was built from."""

    challenge_id: Slug
    at: AwareDatetime
    #: The improver run that proposed the change, and the change.
    run_id: str
    change: ImproverChange
    #: The proposal's issue (``owner/repo#N``), where a maintainer approves it.
    issue: str
    champion: ImproverVariant
    challenger: ImproverVariant
    trials: tuple[SnapshotTrial, ...]
    #: Won on every snapshot.
    outcome: Literal["won", "lost"]


__all__ = [
    "ChallengeRecord",
    "ChampionState",
    "ImproverVariant",
    "Promotion",
    "SnapshotTrial",
]
