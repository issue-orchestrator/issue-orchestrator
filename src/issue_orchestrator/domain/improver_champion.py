"""The champion/challenger rules, pure (#8001).

* :func:`invited` decides, from the run's id alone, whether a run is
  invited to propose a change (about one in ten): the orchestrator decides,
  never the agent, and a replayed run gets the same answer.
* :func:`challenger_of` builds the challenger: the champion with the one
  change applied, refused if the change does not apply to THIS champion
  (a prompt passage it lacks, or a change to what it already is).
* :func:`challenger_won` reads a tournament's verdict: the challenger is
  told apart above the champion, by the same noise band and heat test that
  rank every tournament.
* :func:`promotion_refusals` lists why a challenge may not replace the
  champion: it must have won on every snapshot, been tried against the
  current champion, and its issue approved by a maintainer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from ..contracts.improver_findings import (
    AgentEdit,
    BudgetEdit,
    HeatsEdit,
    ImproverChange,
    ModeEdit,
    PromptEdit,
)
from ..contracts.improver_run import ImproverAgentChoice, ImproverProvider
from ..contracts.improver_toolbox import ImproverMode
from ..contracts.improver_tournament import TournamentResult
from ..contracts.improver_variant import ChallengeRecord, ImproverVariant
from .tech_lead_approval import (
    APPROVED_LABEL,
    MAINTAINER_ROLES,
    ApprovalVerdict,
    ApprovalVerdictKind,
    LabelEvent,
)

#: About one run in ten is invited to propose a change to the improver.
INVITATION_RATE = 0.1
#: The effect id of a run's proposed change (never a finding's: not a slug).
CHANGE_ID = "@improver-change"
#: The tournament arms a challenge runs.
CHAMPION_ARM = "champion"
CHALLENGER_ARM = "challenger"


class ChangeNotApplicable(ValueError):
    """A proposed change that does not apply to the current champion."""


#: How long a live improver run may take: its heats in one wave, each up to
#: its agent timeout (inside the budgeted suite's own timeout).
RUN_BUDGET_MINUTES = 105
#: A scripted agent's timeout, and the least any agent gets.
BASE_AGENT_TIMEOUT_MINUTES = 90
#: An empowered agent's timeout beyond its investigation budget.
TIMEOUT_MARGIN_MINUTES = 15
#: The largest empowered budget a live run can carry.
MAX_BUDGET_MINUTES = RUN_BUDGET_MINUTES - TIMEOUT_MARGIN_MINUTES


def agent_timeout_minutes(mode: ImproverMode, budget_minutes: int) -> int:
    """The live agent timeout for a mode and budget: never mid-budget."""
    if mode is ImproverMode.EMPOWERED:
        return max(BASE_AGENT_TIMEOUT_MINUTES, budget_minutes + TIMEOUT_MARGIN_MINUTES)
    return BASE_AGENT_TIMEOUT_MINUTES


@dataclass(frozen=True)
class LiveLimits:
    """How a variant runs live: all its heats at once, each under its timeout."""

    parallel_heats: int
    agent_timeout_minutes: int
    run_budget_minutes: int = RUN_BUDGET_MINUTES


def run_limits(
    mode: ImproverMode, budget_minutes: int, heats: int, *, agent_timeout: int | None, parallel: int | None
) -> LiveLimits:
    """A run's limits: what the operator set, else the live rule's (all
    heats in one wave; an agent timeout never shorter than its budget)."""
    return LiveLimits(
        parallel_heats=heats if parallel is None else parallel,
        agent_timeout_minutes=agent_timeout_minutes(mode, budget_minutes) if agent_timeout is None else agent_timeout,
    )


def live_limits(variant: ImproverVariant) -> LiveLimits:
    """The limits a live run of ``variant`` uses, or why it cannot run."""
    timeout = agent_timeout_minutes(variant.mode, variant.budget_minutes)
    if timeout > RUN_BUDGET_MINUTES:
        raise ChangeNotApplicable(
            f"a {variant.budget_minutes}-minute budget needs a {timeout}-minute agent, beyond the"
            f" {RUN_BUDGET_MINUTES}-minute run budget (at most {MAX_BUDGET_MINUTES} minutes)"
        )
    return LiveLimits(parallel_heats=variant.heats, agent_timeout_minutes=timeout)


@dataclass(frozen=True)
class ChangeInvitation:
    """A run invited to propose one change to this champion (its prompt is
    the text the run itself ran with)."""

    champion: ImproverVariant
    prompt: str

    def __post_init__(self) -> None:
        if prompt_digest(self.prompt) != self.champion.prompt_sha256:
            raise ValueError("an invitation's prompt is the champion's own")

    def to_json(self) -> str:
        return json.dumps({"champion": self.champion.model_dump(mode="json"), "prompt": self.prompt}, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> ChangeInvitation:
        doc = json.loads(text)
        return cls(ImproverVariant.model_validate(doc["champion"]), doc["prompt"])


def invited(run_id: str, *, rate: float = INVITATION_RATE) -> bool:
    """Whether the run with this id is invited to propose a change."""
    if not 0.0 <= rate <= 1.0:
        raise ValueError(f"an invitation rate is a share of runs, not {rate}")
    draw = int.from_bytes(hashlib.sha256(run_id.encode("utf-8")).digest()[:8], "big") / 2**64
    return draw < rate


def prompt_digest(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def challenger_of(
    champion: ImproverVariant, champion_prompt: str, change: ImproverChange
) -> tuple[ImproverVariant, str]:
    """The challenger (and its prompt): the champion with ``change`` applied."""
    if prompt_digest(champion_prompt) != champion.prompt_sha256:
        raise ValueError("the champion's prompt text does not match its digest")
    edit = change.edit
    prompt = champion_prompt
    match edit:
        case PromptEdit(find=find, replace=replace):
            occurrences = champion_prompt.count(find)
            if occurrences != 1:
                raise ChangeNotApplicable(
                    f"the prompt edit's passage occurs {occurrences} time(s) in the champion's prompt, not once"
                )
            prompt = champion_prompt.replace(find, replace)
            challenger = champion.model_copy(update={"prompt_sha256": prompt_digest(prompt)})
        case AgentEdit(provider=provider, model=model):
            agent = ImproverAgentChoice(provider=ImproverProvider(provider), model=model)
            challenger = champion.model_copy(update={"agent": agent})
        case ModeEdit(mode=mode):
            challenger = champion.model_copy(update={"mode": ImproverMode(mode)})
        case HeatsEdit(heats=heats):
            challenger = champion.model_copy(update={"heats": heats})
        case BudgetEdit(minutes=minutes):
            challenger = champion.model_copy(update={"budget_minutes": minutes})
    if challenger == champion:
        raise ChangeNotApplicable("the change leaves the champion as it is")
    challenger = ImproverVariant.model_validate(challenger.model_dump())
    live_limits(challenger)  # a challenger that could win must be able to run live
    return challenger, prompt


def challenger_won(result: TournamentResult) -> bool:
    """Whether the tournament told the challenger apart ABOVE the champion."""
    by_pair = {(c.higher, c.lower): c for c in result.comparisons}
    if (CHALLENGER_ARM, CHAMPION_ARM) in by_pair:
        return by_pair[(CHALLENGER_ARM, CHAMPION_ARM)].distinguishable
    if (CHAMPION_ARM, CHALLENGER_ARM) in by_pair:
        return False
    raise ValueError(f"tournament {result.tournament_id} did not compare {CHALLENGER_ARM} with {CHAMPION_ARM}")


def promotion_refusals(
    challenge: ChallengeRecord, *, current: ImproverVariant, approval: ApprovalVerdict
) -> list[str]:
    """Every reason the challenge may not replace ``current`` (none: it may)."""
    refusals: list[str] = []
    if challenge.outcome != "won":
        refusals.append("the challenger did not win against the champion on every snapshot")
    if not challenge.trials:
        refusals.append("the challenger was never tried")
    if challenge.champion != current:
        refusals.append(
            f"the challenger was tried against {challenge.champion.id}, but the champion is now {current.id}:"
            " try it again against the current champion"
        )
    if not approval.approved:
        refusals.append(f"its issue {challenge.issue} is not approved: {approval.describe()}")
    return refusals


@dataclass(frozen=True)
class ChallengerIssueFacts:
    """What GitHub shows of a challenger's issue, read fresh at promotion."""

    number: int
    #: None: the issue could not be read.
    state: str | None
    labels: frozenset[str]
    #: The newest event adding, and removing, the ``approved`` label.
    approved_added: LabelEvent | None
    approved_removed: LabelEvent | None
    #: The approver's repository role (None: not a collaborator, or unread).
    approver_role: str | None
    #: The issue was closed at or after the approval (a reopen voids it).
    closed_since_approval: bool
    #: Its body carries the challenger's own marker: it is the issue the
    #: improver filed for THIS change, not one that merely names its token.
    carries_marker: bool


def judge_challenger_issue(facts: ChallengerIssueFacts) -> ApprovalVerdict:
    """#7906's positive approval on a challenger's issue: the ``approved``
    label is on it, its standing labeled event is a person's with a
    maintainer role, and nothing voided it since (removal, closing)."""
    n = facts.number
    if facts.state != "open" or facts.closed_since_approval:
        return ApprovalVerdict(n, ApprovalVerdictKind.CLOSED)
    if not facts.carries_marker:
        # Not the challenger's issue: an approval there approves something else.
        return ApprovalVerdict(n, ApprovalVerdictKind.NOT_CLAIMED)
    if APPROVED_LABEL not in {label.casefold() for label in facts.labels}:
        return ApprovalVerdict(n, ApprovalVerdictKind.NOT_CLAIMED)
    added, removed = facts.approved_added, facts.approved_removed
    if added is None or (removed is not None and removed.event_id > added.event_id):
        return ApprovalVerdict(n, ApprovalVerdictKind.NO_LABEL_EVENT)
    if added.actor_is_bot:
        return ApprovalVerdict(n, ApprovalVerdictKind.BOT_ACTOR, added.actor_login, added.event_id)
    if facts.approver_role not in MAINTAINER_ROLES:
        return ApprovalVerdict(n, ApprovalVerdictKind.NOT_A_MAINTAINER, added.actor_login, added.event_id)
    return ApprovalVerdict(n, ApprovalVerdictKind.MAINTAINER, added.actor_login, added.event_id)


def outcome_of(won: Sequence[bool]) -> str:
    return "won" if won and all(won) else "lost"


__all__ = [
    "CHALLENGER_ARM",
    "CHANGE_ID",
    "CHAMPION_ARM",
    "INVITATION_RATE",
    "ChallengerIssueFacts",
    "ChangeInvitation",
    "ChangeNotApplicable",
    "LiveLimits",
    "MAX_BUDGET_MINUTES",
    "RUN_BUDGET_MINUTES",
    "agent_timeout_minutes",
    "live_limits",
    "run_limits",
    "challenger_of",
    "challenger_won",
    "invited",
    "judge_challenger_issue",
    "outcome_of",
    "prompt_digest",
    "promotion_refusals",
]
