"""One improver run, as the orchestrator records it (#7490).

The record an operator (``improver status``) and the NEXT run read: how the
run ended and why, the stall point it graded each finding at, how those grades
moved since the previous accepted run, and what became of each effect the
orchestrator owes GitHub for it. A rejected run carries the validator's
reasons and owes nothing: a findings file is applied whole or not at all.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .improver_findings import AnomalyKeyRef, Trend

IMPROVER_RUN_SCHEMA_VERSION = 1


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunOutcome(StrEnum):
    #: The findings passed every rule; their effects are owed.
    ACCEPTED = "accepted"
    #: The findings broke a rule; ``rejections`` says which. Nothing is applied.
    REJECTED = "rejected"
    #: An input the improver cannot run without could not be staged.
    UNAVAILABLE = "unavailable"
    #: The agent did not finish (it failed, timed out, or wrote nothing usable).
    AGENT_FAILED = "agent_failed"

    @property
    def exit_code(self) -> int:
        """The budgeted-validation convention: 0 green, 1 a failure, 75 unavailable."""
        return {RunOutcome.ACCEPTED: 0, RunOutcome.REJECTED: 1}.get(self, 75)


class ImproverProvider(StrEnum):
    """The agent CLI an improver run is launched on."""

    #: ``claude -p``, the prompt on stdin.
    CLAUDE = "claude"
    #: ``codex exec``.
    CODEX = "codex"


class ImproverAgentChoice(_Closed):
    """Which provider and model an improver run used (#8001).

    The default is the latest improver tournament's winner
    (:data:`DEFAULT_IMPROVER_AGENT`), so the choice is recorded on each run:
    grades and trends compare like with like.
    """

    provider: ImproverProvider
    model: str = Field(min_length=1)

    @classmethod
    def for_provider(cls, provider: ImproverProvider, model: str | None = None) -> ImproverAgentChoice:
        """``provider`` on ``model``, or on the provider's default model."""
        return cls(provider=provider, model=model or DEFAULT_IMPROVER_MODELS[provider])

    def describe(self) -> str:
        return f"{self.provider.value}:{self.model}"


#: Each provider's default model. Claude's is the ``opus`` alias (its latest
#: Opus); Codex's is pinned, because Codex's own default is not the model the
#: improver was tuned on.
DEFAULT_IMPROVER_MODELS: dict[ImproverProvider, str] = {
    ImproverProvider.CLAUDE: "opus",
    ImproverProvider.CODEX: "gpt-5.6-sol",
}

#: The improver tournament's winner (2026-10-04, #8001: Claude Opus 3.8 of
#: 24 against Codex's 0.2, on both graders). Change it only on a new
#: tournament's result.
DEFAULT_IMPROVER_AGENT = ImproverAgentChoice(
    provider=ImproverProvider.CLAUDE, model=DEFAULT_IMPROVER_MODELS[ImproverProvider.CLAUDE]
)


class EffectStatus(StrEnum):
    #: Not applied yet (never tried, or stopped by a rate limit).
    PENDING = "pending"
    #: A new issue was filed.
    FILED = "filed"
    #: Evidence was commented on an existing issue (the tracked one, or an
    #: open issue an earlier run filed for the same finding).
    COMMENTED = "commented"


class EffectReceipt(_Closed):
    """What the orchestrator did on GitHub for one accepted finding."""

    finding_id: str
    #: The finding's identity across runs (output + anomaly keys + case id);
    #: an open issue whose title carries it is the same finding.
    key: str
    status: EffectStatus
    issue_number: int | None = None
    at: AwareDatetime | None = None
    detail: str = ""
    #: The last application error that was not a rate limit (a rate limit
    #: only defers). Kept on the receipt until an application succeeds.
    error: str | None = None
    #: Set, durably, just before an issue is created: a POST whose result was
    #: lost may still have filed it, so the next attempt first proves by the
    #: body marker whether it did, and never POSTs twice.
    create_attempted_at: AwareDatetime | None = None


class FindingGrade(_Closed):
    finding_id: str
    anomaly_keys: tuple[AnomalyKeyRef, ...]
    classification: str
    stall_point: str
    output: str


class StallPointMove(_Closed):
    """How many findings graded at one stall point, now and in the previous accepted run."""

    stall_point: str
    previous: int | None
    current: int


class ExamScore(_Closed):
    """The latest exam result of one case, as the run saw it."""

    case_id: str
    passed: bool


class ImproverRunRecord(_Closed):
    schema_version: Literal[1] = IMPROVER_RUN_SCHEMA_VERSION
    run_id: str = Field(min_length=1)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    outcome: RunOutcome
    #: Why the run ended as it did, in plain words.
    detail: str
    #: The audited engine's Control Center key (two engines may work one repo).
    engine_id: str = Field(min_length=1)
    audited_repo: str
    outputs_repo: str
    #: The run's working directory (``improver-data/``, the findings, the
    #: agent's final message), so an operator can read what it saw.
    run_dir: str
    #: The provider and model the agent ran on. ``None`` only on a run
    #: recorded before the improver's model was pluggable (#8001), which ran
    #: on Codex.
    agent: ImproverAgentChoice | None = None
    #: Set once staging read the engine's start record.
    engine_commit: str | None = None
    #: Whether ``run_dir`` holds a staged ``audit.json`` the next run can diff against.
    audit_staged: bool = False
    rejections: tuple[str, ...] = ()
    grades: tuple[FindingGrade, ...] = ()
    stall_points: tuple[StallPointMove, ...] = ()
    trend: Trend | None = None
    #: The latest exam result of each case the run saw.
    exam_scores: tuple[ExamScore, ...] = ()
    effects: tuple[EffectReceipt, ...] = ()
    #: A BLIND run's hidden open issues (``--exclude-open-issue``): it was
    #: staged without them, to test whether the improver finds what they
    #: track. Its findings are graded and recorded, never filed: a blind run
    #: owes GitHub nothing, and a later run does not compare its grades.
    blind_excluded_issues: tuple[int, ...] = ()
    #: Earlier runs whose effects were still owed when this run finished:
    #: a run is not green while the improver owes GitHub anything.
    owed_by_earlier_runs: tuple[str, ...] = ()

    @property
    def is_engine_audit(self) -> bool:
        """Whether this run counts as an audit of its engine: accepted, and
        not blind. A blind run saw a doctored open-issue list, so it is never
        the sweep's "already audited" mark nor the next run's baseline."""
        return self.outcome is RunOutcome.ACCEPTED and not self.blind_excluded_issues

    @property
    def pending_effects(self) -> tuple[EffectReceipt, ...]:
        return tuple(e for e in self.effects if e.status is EffectStatus.PENDING)

    @property
    def exit_code(self) -> int:
        """The outcome's code, except that an accepted run whose effects are
        not all applied (failed, deferred by a rate limit, or stopped behind
        an earlier run's) is unavailable: its findings stand and the next
        run or ``improver apply`` applies them."""
        if self.outcome is RunOutcome.ACCEPTED and (self.pending_effects or self.owed_by_earlier_runs):
            return 75
        return self.outcome.exit_code


__all__ = [
    "DEFAULT_IMPROVER_AGENT",
    "DEFAULT_IMPROVER_MODELS",
    "IMPROVER_RUN_SCHEMA_VERSION",
    "EffectReceipt",
    "EffectStatus",
    "ExamScore",
    "FindingGrade",
    "ImproverAgentChoice",
    "ImproverProvider",
    "ImproverRunRecord",
    "RunOutcome",
    "StallPointMove",
]
