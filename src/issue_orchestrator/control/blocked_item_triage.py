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
fingerprint at that triage. An item whose blocking state changed, or whose last
triage did not take effect, is owed another.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from ..domain.blocked_item_triage import (
    MAX_TRIAGE_ITEMS_PER_RUN,
    TRIAGE_IN_FORCE_EFFECTS,
    PriorTriage,
    TriageAgenda,
    TriageAgendaItem,
    block_fingerprint,
)
from ..domain.session_kind import SessionKind
from ..domain.tech_lead_session import (
    PROPOSED_TECH_LEAD_LABEL,
    TECH_LEAD_OBSERVATION_LABEL,
    TechLeadSessionFlavor,
)
from ..events import EventName

if TYPE_CHECKING:
    from ..domain.human_block import NeedsHumanCause
    from ..domain.models import OrchestratorState
    from ..domain.tech_lead_artifacts import TechLeadDecision
    from ..domain.tech_lead_charter_decisions import TechLeadCharterDecision
    from ..domain.tech_lead_session import TechLeadLaunchAuthority
    from ..infra.config import Config
    from ..ports.issue import Issue
    from ..ports.tech_lead_charter_ledger import TechLeadCharterDecisionReader
    from ..ports.timeline_store import TimelineRecord
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

#: How many of an item's decisions are read to find its latest triage.
_DECISIONS_PER_ITEM = 50
#: How many timeline records are read to find an item's latest agent question.
_TIMELINE_RECORDS_PER_ITEM = 50

_MACHINERY = frozenset(
    {PROPOSED_TECH_LEAD_LABEL.casefold(), TECH_LEAD_OBSERVATION_LABEL.casefold()}
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
        timeline_reader: Callable[[int, int], Sequence["TimelineRecord"]],
    ) -> None:
        self._config = config
        self._state = state
        self._labels = labels
        self._needs_human_causes = needs_human_causes
        self._ledger = charter_ledger
        self._timeline = timeline_reader

    def agenda(self, *, anchor_issue_number: int) -> TriageAgenda:
        """Every blocked item owed a triage, oldest first, capped per run.

        A ledger or cause-store read that fails raises: the agenda is a
        required launch input, and guessing it would either churn items whose
        triage is in force or silently skip ones that are owed one.
        """
        blocked = [
            issue
            for issue in self._scope_issues()
            if issue.number != anchor_issue_number and self._blocking(issue) is not None
        ]
        causes = self._needs_human_causes([issue.number for issue in blocked])
        owed: list[TriageAgendaItem] = []
        in_force: list[int] = []
        for issue in sorted(blocked, key=lambda item: item.number):
            item = self._item(issue, causes.get(issue.number, frozenset()))
            if item is None:
                in_force.append(issue.number)
            else:
                owed.append(item)
        return TriageAgenda(
            items=tuple(owed[:MAX_TRIAGE_ITEMS_PER_RUN]),
            in_force=tuple(in_force),
            deferred=tuple(item.issue_number for item in owed[MAX_TRIAGE_ITEMS_PER_RUN:]),
        )

    # -- per item ---------------------------------------------------------------

    def _scope_issues(self) -> Sequence["Issue"]:
        return scope_issues(self._state())

    def _blocking(self, issue: "Issue") -> tuple[tuple[str, ...], bool] | None:
        return blocked_work_item(issue, self._labels, self._config.tech_lead_review_agent)

    def _item(
        self, issue: "Issue", causes: "frozenset[NeedsHumanCause]"
    ) -> TriageAgendaItem | None:
        blocking = self._blocking(issue)
        assert blocking is not None
        labels, marker = blocking
        fingerprint = block_fingerprint(
            labels, tech_lead_marker=marker, needs_human_label=self._labels.needs_human
        )
        prior = self._prior(issue.number)
        if prior is not None and prior.fingerprint == fingerprint and (
            prior.effect in TRIAGE_IN_FORCE_EFFECTS
        ):
            return None
        return TriageAgendaItem(
            issue_number=issue.number,
            title=issue.title,
            labels=tuple(issue.labels),
            blocking_labels=labels,
            needs_human_causes=tuple(sorted(cause.value for cause in causes)),
            fingerprint=fingerprint,
            agent_question=self._agent_question(issue.number),
            reason=_owed_reason(prior, fingerprint),
            prior=prior,
        )

    def _prior(self, issue_number: int) -> PriorTriage | None:
        latest = _latest_triage(self._ledger.list_about_issue(issue_number, limit=_DECISIONS_PER_ITEM), issue_number)
        if latest is None:
            return None
        assert latest.triage_class is not None and latest.triage_fingerprint is not None
        return PriorTriage(
            triage_class=latest.triage_class,
            action_kind=latest.action_kind,
            effect=latest.effect,
            decided_at=latest.decided_at,
            fingerprint=latest.triage_fingerprint,
        )

    def _agent_question(self, issue_number: int) -> str | None:
        """The last question an agent put to a human about the item (best-effort)."""
        try:
            records = self._timeline(issue_number, _TIMELINE_RECORDS_PER_ITEM)
        except Exception:
            logger.warning(
                "[TRIAGE] timeline of #%d unreadable; its agenda item carries no question",
                issue_number, exc_info=True,
            )
            return None
        for record in reversed(tuple(records)):
            question = record.data.get("question") if record.event == EventName.ISSUE_NEEDS_HUMAN.value else None
            if isinstance(question, str) and question.strip():
                return question.strip()
        return None


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
    or changed block makes the board worth reviewing again.
    """
    from .label_manager import LabelManager

    labels = LabelManager(config)
    found: list[tuple[int, str]] = []
    for issue in scope_issues(state):
        blocking = blocked_work_item(issue, labels, config.tech_lead_review_agent)
        if blocking is not None:
            found.append((issue.number, block_fingerprint(
                blocking[0], tech_lead_marker=blocking[1], needs_human_label=labels.needs_human,
            )))
    return tuple(sorted(found))


def _latest_triage(
    decisions: Sequence["TechLeadCharterDecision"], issue_number: int
) -> "TechLeadCharterDecision | None":
    """The newest decision that triaged *issue_number* (the ledger reads newest first)."""
    return next(
        (
            decision
            for decision in decisions
            if decision.triage_class is not None and decision.target_number == issue_number
        ),
        None,
    )


def _owed_reason(prior: PriorTriage | None, fingerprint: str) -> str:
    if prior is None:
        return "never triaged"
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

