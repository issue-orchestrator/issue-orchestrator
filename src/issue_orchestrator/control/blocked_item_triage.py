"""The ONE owner of blocked-item triage (#7593): who is owed one, and was it given.

Two questions, one module, so the rule that grants an item and the rule that
checks the decision against the grant cannot drift:

* :class:`StateBlockedItemTriage` builds a health review's **agenda** at launch:
  every open blocked work item in scope whose blocking state has no triage in
  force. Its facts are the engine's own (the tick's cached issues, the shared
  needs-human block's causes, the charter decision ledger, the timeline), read
  without a GitHub call. The agenda's grants are recorded in the run's launch
  authority, and the agent reads the agenda from ``blocked-item-triage.json``.
* :func:`triage_coverage_violation` is the completion rule: the decision must
  give every granted item exactly one action carrying a ``triage_class``, and
  carry none for anything else.

"In force" is read from the charter record the triaging action left
(:data:`~..domain.blocked_item_triage.TRIAGE_IN_FORCE_EFFECTS`), with the item's
fingerprint at that triage. A triage awaiting approval is in force while a gated
proposal of its kind for the item is OPEN, read from the durable op ledger (the
proposal's own record), so a best-effort link of the issue number onto the
charter record never decides it. An item whose blocking state changed, or whose
last triage did not take effect, is owed another.

The blocking state includes the block's EPISODE (#8688): a needs-human block
lifted and later re-raised under the same label and cause is a new episode,
read from the generation its one owner records when it puts the label on
afresh (:class:`~..ports.pending_work_claim_store.NeedsHumanEpisodeReader`),
not from the best-effort timeline (#8697), and bound to GitHub's label events
by :class:`~.needs_human_episodes.NeedsHumanEpisodes`. A block whose episode is
unrecorded or cannot be verified is owed a triage, never covered: the rule
fails toward triaging again, not silence.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.standing_ruling import ruling_in_full
from ..domain.blocked_item_triage import (
    MAX_TRIAGE_ITEMS_PER_RUN,
    UNKNOWN_EPISODE,
    PriorTriage,
    TriageAgenda,
    TriageAgendaItem,
    block_fingerprint,
)
from ..domain.session_kind import SessionKind
from ..domain.tech_lead_approval import APPROVAL_MODEL_LABELS
from ..domain.tech_lead_session import (
    TECH_LEAD_OBSERVATION_LABEL,
    TechLeadSessionFlavor,
)
from ..events import EventName

if TYPE_CHECKING:
    from ..domain.human_block import NeedsHumanCause
    from ..domain.models import OrchestratorState
    from ..domain.standing_ruling import StandingRuling
    from ..domain.tech_lead_artifacts import TechLeadDecision
    from ..domain.tech_lead_session import TechLeadLaunchAuthority
    from ..infra.config import Config
    from ..ports.issue import Issue
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from ..ports.tech_lead_charter_ledger import TechLeadCharterDecisionReader
    from .needs_human_episodes import NeedsHumanEpisodes
    from ..ports.timeline_store import TimelineRecord
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

#: How many timeline records are read to find an item's latest agent question.
_TIMELINE_RECORDS_PER_ITEM = 50

_MACHINERY = frozenset(
    {label.casefold() for label in APPROVAL_MODEL_LABELS}
    | {TECH_LEAD_OBSERVATION_LABEL.casefold()}
)


class StateBlockedItemTriage:
    """Builds a health review's triage agenda from the engine's own owners."""

    def __init__(
        self,
        *,
        config: "Config",
        state: Callable[[], "OrchestratorState"],
        labels: "LabelManager",
        needs_human_causes: Callable[
            [Sequence[int]], Mapping[int, "frozenset[NeedsHumanCause]"]
        ],
        charter_ledger: "TechLeadCharterDecisionReader",
        open_proposals: Callable[[], "OpenProposals"],
        timeline_reader: Callable[[int, int], Sequence["TimelineRecord"]],
        standing_rulings: Callable[[int], Sequence["StandingRuling"]],
        episodes: "NeedsHumanEpisodes",
    ) -> None:
        self._config = config
        self._state = state
        self._labels = labels
        self._needs_human_causes = needs_human_causes
        self._ledger = charter_ledger
        self._open_proposals = open_proposals
        self._timeline = timeline_reader
        self._standing_rulings = standing_rulings
        self._episodes = episodes

    def agenda(self, *, anchor_issue_number: int) -> TriageAgenda:
        """Every blocked item owed a triage, oldest first, capped per run.

        A ledger or cause-store read that fails raises: the agenda is a
        required launch input, and guessing it would either churn items whose
        triage is in force or silently skip ones that are owed one.
        """
        owed, in_force = owed_triages(
            self._config, self._state(), self._labels, self._ledger,
            open_proposals=self._open_proposals(), exclude=frozenset({anchor_issue_number}),
            # Every grant pins an episode bound to GitHub's label events (#8688).
            episodes=self._episodes.verified,
        )
        causes = self._needs_human_causes([item.issue.number for item in owed])
        items = [
            TriageAgendaItem(
                issue_number=item.issue.number,
                title=item.issue.title,
                labels=tuple(item.issue.labels),
                blocking_labels=item.blocking_labels,
                needs_human_causes=tuple(
                    sorted(cause.value for cause in causes.get(item.issue.number, frozenset()))
                ),
                fingerprint=item.fingerprint,
                agent_question=self._agent_question(item.issue.number),
                reason=_owed_reason(item.prior, item.fingerprint),
                prior=item.prior,
                standing_rulings=tuple(
                    ruling_in_full(ruling) for ruling in self._standing_rulings(item.issue.number)
                ),
            )
            for item in owed[:MAX_TRIAGE_ITEMS_PER_RUN]
        ]
        return TriageAgenda(
            items=tuple(items),
            in_force=in_force,
            deferred=tuple(item.issue.number for item in owed[MAX_TRIAGE_ITEMS_PER_RUN:]),
        )

    def _agent_question(self, issue_number: int) -> str | None:
        return latest_agent_question(self._timeline, issue_number)


def latest_agent_question(
    timeline: Callable[[int, int], Sequence["TimelineRecord"]], issue_number: int
) -> str | None:
    """The last question an agent put to a human about the item (best-effort).

    The agenda tolerates an unreadable timeline; a resolution's human-only
    screen reads EVERY question of the whole timeline through
    :func:`agent_questions_in` and fails instead (#7658).
    """
    try:
        records = timeline(issue_number, _TIMELINE_RECORDS_PER_ITEM)
    except Exception:
        logger.warning(
            "[TRIAGE] timeline of #%d unreadable; no agent question is read for it",
            issue_number, exc_info=True,
        )
        return None
    return agent_question_in(records)


def agent_questions_in(records: Sequence["TimelineRecord"]) -> tuple[str, ...]:
    """Every agent question among *records*, oldest first."""
    found = (
        record.data.get("question")
        for record in records
        if record.event == EventName.ISSUE_NEEDS_HUMAN.value
    )
    return tuple(question.strip() for question in found if isinstance(question, str) and question.strip())


def agent_question_in(records: Sequence["TimelineRecord"]) -> str | None:
    """The latest agent question among *records* (oldest first), else None."""
    questions = agent_questions_in(records)
    return questions[-1] if questions else None


@dataclass(frozen=True)
class OwedTriage:
    """A blocked item whose block has no triage in force."""

    issue: "Issue"
    blocking_labels: tuple[str, ...]
    fingerprint: str
    prior: PriorTriage | None


def owed_triages(
    config: "Config",
    state: "OrchestratorState",
    labels: "LabelManager",
    ledger: "TechLeadCharterDecisionReader",
    *,
    open_proposals: "OpenProposals",
    episodes: Callable[[Mapping[int, Sequence[str]]], Mapping[int, str]],
    exclude: frozenset[int] = frozenset(),
) -> tuple[list[OwedTriage], tuple[int, ...]]:
    """``(owed, in force)``: every blocked work item in scope, oldest first,
    split by whether its block has a triage in force (#7593).

    THE one rule both the agenda (who is granted) and the health-review
    trigger (is a review owed) read, so an item deferred by the per-run cap or
    whose triage did not take effect keeps a review due. ``episodes`` reads
    the needs-human episode of each item (by its labels) whose block holds
    that label or the hand-over marker (#8688).
    """
    blocked = [
        (issue, blocking)
        for issue in sorted(scope_issues(state), key=lambda item: item.number)
        if issue.number not in exclude
        and (blocking := blocked_work_item(issue, labels, config.tech_lead_review_agent)) is not None
    ]
    held = {
        issue.number: tuple(issue.labels)
        for issue, blocking in blocked if _holds_needs_human(blocking, labels)
    }
    recorded = episodes(held) if held else {}
    owed: list[OwedTriage] = []
    in_force: list[int] = []
    for issue, (names, marker) in blocked:
        fingerprint = block_fingerprint(
            names, tech_lead_marker=marker, needs_human_label=labels.needs_human,
            episode=recorded.get(issue.number, UNKNOWN_EPISODE) if issue.number in held else None,
        )
        prior = prior_triage(ledger, issue.number, open_proposals)
        if prior is not None and prior.covers(fingerprint):
            in_force.append(issue.number)
        else:
            owed.append(OwedTriage(issue, names, fingerprint, prior))
    return owed, tuple(in_force)


def _holds_needs_human(blocking: tuple[tuple[str, ...], bool], labels: "LabelManager") -> bool:
    """The block is (in part) the shared needs-human block, whose episodes are recorded."""
    names, marker = blocking
    return marker or labels.needs_human.casefold() in {name.casefold() for name in names}


def triage_owed(
    config: "Config",
    state: "OrchestratorState",
    authority: "TechLeadAuthorityStore",
    episodes: "NeedsHumanEpisodes",
) -> bool:
    """Whether any blocked work item in scope is owed a triage (#7593).

    It runs on the tick, so it reads the recorded episodes, re-verified
    against GitHub at most once per recheck period (#8688).
    """
    from .label_manager import LabelManager

    owed, _ = owed_triages(
        config, state, LabelManager(config), authority.charter_ledger,
        open_proposals=open_proposal_index(authority), episodes=episodes.current,
    )
    return bool(owed)


@dataclass(frozen=True)
class OpenProposals:
    """Every open gated proposal, by the decision that filed it (#7593 r4/r6)."""

    #: ``(source run, source action) -> proposal issue``: the decision that
    #: filed each open proposal (its op's own identity).
    by_source: Mapping[tuple[str, str], int]
    #: Every open proposal issue, for a record linked to one it joined.
    numbers: frozenset[int]

    def for_record(self, run_id: str, action_id: str, linked: int | None) -> int | None:
        """The open proposal a triage record's decision is waiting on, else None."""
        filed = self.by_source.get((run_id, action_id))
        if filed is not None:
            return filed
        return linked if linked in self.numbers else None


def open_proposal_index(authority: "TechLeadAuthorityStore") -> OpenProposals:
    """Every open gated proposal in the durable op ledger (#7593 review r4)."""
    ops = authority.list_ops()
    return OpenProposals(
        by_source={(op.source_run_id, op.source_action_id): number for number, op in ops},
        numbers=frozenset(number for number, _op in ops),
    )


def prior_triage(
    ledger: "TechLeadCharterDecisionReader", issue_number: int, open_proposals: OpenProposals
) -> PriorTriage | None:
    """The item's latest triage; one awaiting approval names its OPEN proposal.

    The op ledger is the durable record of a filed proposal, so it, not the
    charter record's best-effort link, says whether the operator has one to
    answer: the proposal THIS decision filed (by the op's source run and
    action), or the one it joined by reuse (its link) while that is open. An
    older proposal for the same item never stands in for a newer decision
    whose filing failed (#7593 review r4/r6).
    """
    latest = ledger.latest_triage_for_issue(issue_number)
    if latest is None:
        return None
    assert latest.triage_class is not None and latest.triage_fingerprint is not None
    awaiting = latest.effect == "awaiting_approval"
    return PriorTriage(
        triage_class=latest.triage_class,
        action_kind=latest.action_kind,
        effect=latest.effect,
        decided_at=latest.decided_at,
        fingerprint=latest.triage_fingerprint,
        proposal_issue_number=(
            open_proposals.for_record(latest.run_id, latest.action_id, latest.proposal_issue_number)
            if awaiting
            else latest.proposal_issue_number
        ),
    )


def scope_issues(state: "OrchestratorState") -> Sequence["Issue"]:
    """The same issue snapshot the dashboard's blocked lane is built from."""
    return state.cached_scope_issues or state.cached_queue_issues


def blocked_work_item(
    issue: "Issue", labels: "LabelManager", tech_lead_agent: str | None
) -> tuple[tuple[str, ...], bool] | None:
    """``(blocking labels, hand-over marker)`` of a blocked work item, else None.

    THE one definition of "a blocked item a health review owes a triage",
    shared by the agenda and by the health-review trigger's board fingerprint.
    """
    if issue.state != "open":
        return None
    if not SessionKind.issue_is_work_item(issue.agent_type, tech_lead_agent):
        return None  # a proposal, case file or tech-lead anchor is not work
    folded = {name.casefold() for name in issue.labels}
    if folded & _MACHINERY:
        return None
    blocking = tuple(labels.get_blocking(issue.labels))
    marker = labels.tech_lead_needs_human.casefold() in folded
    if not blocking and not marker:
        return None
    return blocking, marker


def label_blocked_work_items(
    config: "Config", state: "OrchestratorState"
) -> tuple[tuple[int, str], ...]:
    """Every label-blocked work item in scope with its block fingerprint (#7593).

    The health-review trigger folds these into its board fingerprint, so a new
    or changed block makes the board worth reviewing again. Labels only: a
    re-block under the same labels keeps a review due through
    :func:`triage_owed`, which reads its episode (#8688).
    """
    from .label_manager import LabelManager

    labels = LabelManager(config)
    found: list[tuple[int, str]] = []
    for issue in scope_issues(state):
        blocking = blocked_work_item(issue, labels, config.tech_lead_review_agent)
        if blocking is not None:
            found.append((issue.number, block_fingerprint(
                blocking[0], tech_lead_marker=blocking[1], needs_human_label=labels.needs_human,
                episode=None,
            )))
    return tuple(sorted(found))


def _owed_reason(prior: PriorTriage | None, fingerprint: str) -> str:
    if prior is None:
        return "never triaged"
    if fingerprint.endswith(f"@{UNKNOWN_EPISODE}"):
        return (
            f"its block's onset is not recorded or could not be verified against"
            f" GitHub's label events, so its {prior.triage_class.value} triage cannot"
            " be shown to cover this block"
        )
    if prior.fingerprint != fingerprint and (
        prior.fingerprint.partition("@")[0] == fingerprint.partition("@")[0]
    ):
        return (
            f"it was blocked again, under the same labels, since it was triaged"
            f" {prior.triage_class.value} ({prior.fingerprint} -> {fingerprint})"
        )
    if prior.fingerprint != fingerprint:
        return (
            f"its block changed since it was triaged {prior.triage_class.value}"
            f" ({prior.fingerprint or 'none'} -> {fingerprint or 'none'})"
        )
    return (
        f"its last triage ({prior.triage_class.value}, {prior.action_kind}) did not take"
        f" effect: {prior.effect}"
    )


def triage_coverage_violation(
    decision: "TechLeadDecision", authority: "TechLeadLaunchAuthority"
) -> str | None:
    """Every granted item triaged exactly once; nothing else triaged (#7593)."""
    granted = authority.triage_issue_numbers()
    triaged: dict[int, list[str]] = {}
    for action in decision.proposed_actions:
        if action.triage_class is None:
            continue
        if authority.flavor is not TechLeadSessionFlavor.HEALTH_REVIEW or action.target_number not in granted:
            return (
                f"proposed action {action.id} carries triage_class"
                f" {action.triage_class.value} for #{action.target_number}, which this run"
                " was not granted to triage (see blocked-item-triage.json)"
            )
        triaged.setdefault(action.target_number, []).append(action.id)
    duplicated = {number: ids for number, ids in triaged.items() if len(ids) > 1}
    if duplicated:
        number, ids = sorted(duplicated.items())[0]
        return (
            f"blocked item #{number} carries {len(ids)} triage actions ({', '.join(ids)});"
            " give each item exactly one"
        )
    missing = sorted(granted - triaged.keys())
    if missing:
        return (
            "every blocked item in blocked-item-triage.json needs exactly one proposed"
            " action that targets it and carries a triage_class (operator_decision,"
            " human_hand_over, explained or remedy); untriaged: "
            + ", ".join(f"#{number}" for number in missing)
        )
    return None
