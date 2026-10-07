"""The improver tournament's records: frozen snapshots, answer keys, grades, results (#8001).

A tournament runs improver ARMS (a provider, model and mode, or a challenger
variant) on a FROZEN SNAPSHOT of an engine, anonymizes their answers, has
cross-model GRADERS score them against the snapshot's ANSWER KEY, and ranks
the arms. These models are the files it reads and writes.

Answer keys come from hindsight and are never written by the improver:
the sealed key written before a tournament's results, plus problems found
LATER (by the operator, the tech lead or the coordinator) that the evidence
already showed at snapshot time. Only ``confirmed`` items score.
"""

from __future__ import annotations

import re

from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

SLUG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
#: A name that is also one path component (a snapshot, key, tournament, arm, grader).
Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN)]


def require_slug(value: str, what: str) -> str:
    """``value``, if it is one safe path component; a name like
    ``../../snapshots/x`` would write outside its directory."""
    if re.fullmatch(SLUG_PATTERN, value) is None:
        raise ValueError(f"{what} is letters, digits, '.', '_' or '-' (one path component), not {value!r}")
    return value
Grade = Literal["full", "half", "miss"]
#: What one grade earns of an item's weight.
GRADE_CREDIT: dict[str, float] = {"full": 1.0, "half": 0.5, "miss": 0.0}

SNAPSHOT_MANIFEST = "snapshot.json"


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FrozenSnapshot(_Closed):
    """One engine as staged at ``taken_at``, kept unchanged so arms can be
    compared on it at any later time: ``improver-data/`` (the staged bundle)
    and ``toolbox/`` (byte copies of its stores and logs, a clone)."""

    id: Slug
    taken_at: AwareDatetime
    audited_repo: str
    engine_id: str
    engine_commit: str
    #: Where it came from (a run dir, the 2026-10-04 tournament's files).
    origin: str
    #: Each change made to bring old staged inputs to today's contracts.
    upgrades: tuple[str, ...] = ()
    has_toolbox: bool


class AnswerKeyItem(_Closed):
    id: Slug
    weight: Annotated[int, Field(ge=1, le=3)]
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    category: Literal["stall", "design"]
    #: ``sealed_key``: written before any arm's results. ``hindsight``: found
    #: later, its evidence already present at snapshot time.
    source: Literal["sealed_key", "hindsight"]
    #: Only ``confirmed`` items are graded; a ``candidate`` waits for a person.
    status: Literal["confirmed", "candidate"]
    #: Issues that record it (``owner/repo#n``), with when each was filed.
    links: tuple[str, ...] = ()
    filed_at: date | None = None
    added_at: AwareDatetime
    #: Who added it: a person or the coordinator, never the improver.
    added_by: str = Field(min_length=1)


class AnswerKey(_Closed):
    snapshot_id: Slug
    #: The sealed key's own words before its items: what was audited and how
    #: to grade (e.g. "found but mis-diagnosed: half weight"). Graders read it
    #: verbatim; it is part of the key.
    preamble: str = ""
    items: tuple[AnswerKeyItem, ...]

    @property
    def scored(self) -> tuple[AnswerKeyItem, ...]:
        return tuple(i for i in self.items if i.status == "confirmed")

    @property
    def max_score(self) -> int:
        return sum(i.weight for i in self.scored)


class ItemGrade(_Closed):
    grade: Grade
    why: str


class Extra(_Closed):
    id: str
    summary: str


class OutputGrades(_Closed):
    """One grader's grades of one anonymized output."""

    items: dict[str, ItemGrade]
    #: How many findings are unsupported: one per id in ``unsupported_ids``.
    unsupported: Annotated[int, Field(ge=0)]
    unsupported_ids: tuple[str, ...] = ()
    extras: tuple[Extra, ...] = ()

    @model_validator(mode="after")
    def _one_count_per_unsupported_finding(self) -> OutputGrades:
        # A grader naming an unsupported finding but counting 0 would score
        # it free; the count and the ids must say the same thing.
        if len(set(self.unsupported_ids)) != len(self.unsupported_ids):
            raise ValueError(f"unsupported_ids repeat: {list(self.unsupported_ids)}")
        if self.unsupported != len(self.unsupported_ids):
            raise ValueError(
                f"unsupported is {self.unsupported} but unsupported_ids names {len(self.unsupported_ids)} finding(s)"
            )
        return self


class TournamentArm(_Closed):
    """What one arm runs: a provider and model, in a mode."""

    name: Slug
    provider: Literal["claude", "codex"]
    model: str = Field(min_length=1)
    mode: Literal["scripted", "empowered"]


class GraderRun(_Closed):
    name: Slug
    provider: Literal["claude", "codex"]
    model: str
    accepted: bool
    detail: str


class ArmScore(_Closed):
    arm: str
    #: Each output's score, per grader (ascending; a heat with no answer is 0).
    scores: dict[str, tuple[float, ...]]
    mean: float
    low: float
    high: float


class TournamentResult(_Closed):
    tournament_id: str
    snapshot_id: str
    key_items: int
    max_score: int
    graders: tuple[GraderRun, ...]
    arms: tuple[ArmScore, ...]
    #: Best first; arms within the tie margin share a group.
    ranking: tuple[tuple[str, ...], ...]
    tie_margin: float

    def ranking_text(self) -> str:
        return " > ".join(" ≈ ".join(group) for group in self.ranking)


__all__ = [
    "GRADE_CREDIT",
    "SNAPSHOT_MANIFEST",
    "AnswerKey",
    "AnswerKeyItem",
    "ArmScore",
    "Extra",
    "FrozenSnapshot",
    "GraderRun",
    "ItemGrade",
    "OutputGrades",
    "TournamentArm",
    "TournamentResult",
]
