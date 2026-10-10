"""Blocked-item triage (#7593): every blocked item gets one class and one applied action."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.unit.control.decision_steps_fakes import unused_decision_steps
from issue_orchestrator.control.standing_rulings import StandingRulingsOwner
from issue_orchestrator.domain.standing_ruling import StandingRuling
from tests.standing_ruling_helpers import InMemoryStandingRulingsIndex
from issue_orchestrator.domain.tech_lead_approval import AWAITING_APPROVAL_LABEL, LabelEvent
from issue_orchestrator.control.actions import (
    ApplyOperatorDecisionAction,
    CreateTechLeadProposalIssueAction,
)
from issue_orchestrator.control.blocked_item_triage import (
    OpenProposals,
    StateBlockedItemTriage,
    triage_coverage_violation,
)
from issue_orchestrator.control.block_episodes import BlockEpisodes
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.needs_human_episodes import NeedsHumanEpisodes
from issue_orchestrator.domain.issue_disposition_gate import IssueDispositionGateStatus
from issue_orchestrator.control.proposal_dedup_gate import DuplicateTargetGrant, OpenIssueCorpus
from issue_orchestrator.control.reconciliation import build_expected_for_mutation
from issue_orchestrator.control.tech_lead_charter_records import CharterDecisionLog
from issue_orchestrator.control.tech_lead_decision_actions import plan_tech_lead_decision_actions
from issue_orchestrator.control.tech_lead_operator_decision import (
    OperatorDecisionExecutor,
    decision_marker,
    follow_up_marker,
)
from issue_orchestrator.control.tech_lead_proposals import plan_approved_tech_lead_op_executions
from issue_orchestrator.domain.blocked_item_triage import (
    MAX_TRIAGE_ITEMS_PER_RUN,
    BlockEpisode,
    TriageAgenda,
    TriageAgendaItem,
    TriageGrant,
    block_fingerprint,
    render_triage_instructions,
)
from issue_orchestrator.domain.human_block import NeedsHumanCause
from issue_orchestrator.domain.models import Issue, OrchestratorState
from issue_orchestrator.domain.tech_lead_artifacts import (
    DecisionFollowUp,
    ProposedTechLeadAction,
    TRIAGE_CLASS_ACTION_TYPES,
    TechLeadDecision,
    TriageClass,
)
from issue_orchestrator.domain.tech_lead_charter import (
    CharterAuthority,
    CharterBinding,
    CharterDepth,
    CharterOutcome,
    CharterReason,
    CharterRole,
)
from issue_orchestrator.domain.tech_lead_charter_decisions import (
    CharterDecisionSource,
    CharterExecutionResult,
    CharterProposalLifecycle,
    TechLeadCharterDecision,
)
from issue_orchestrator.domain.tech_lead_session import (
    ApprovedTechLeadOp,
    OperatorDecision,
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.control.tech_lead_reset_retry import STALE_DOWNGRADE_MODE
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.operator_decision_retries import DecisionRetryState
from issue_orchestrator.ports.operator_issue_commands import (
    OperatorCommandIntent,
    OperatorCommandOutcome,
    OperatorCommandStatus,
)
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore
from issue_orchestrator.ports.timeline_store import TimelineRecord

ANCHOR = 900
SPLIT_QUESTION = (
    "#262 is more than one session. Its share-page slice is done and gate-green;"
    " the live D1 seller index is not started. Should I split #262: land this"
    " branch as a PR under 'Refs #262' and move the live index into its own issue(s)?"
)


def _config() -> Config:
    config = Config()
    config.repo = "porchpin/porchpin"
    config.tech_lead_review_agent = "agent:tech-lead"
    return config


def _issue(number: int, *labels: str, title: str | None = None) -> Issue:
    return Issue(
        number=number, title=title or f"Issue {number}", labels=list(labels),
        repo="porchpin/porchpin", state="open",
    )


@dataclass
class _Ledger:
    decisions: dict[int, list[TechLeadCharterDecision]] = field(default_factory=dict)

    def latest_triage_for_issue(self, issue_number: int):
        return next(iter(self.decisions.get(issue_number, ())), None)


def _triage_record(
    issue: int, triage_class: TriageClass, fingerprint: str, *, effect: str, filed: bool = True,
) -> TechLeadCharterDecision:
    executed = effect in {"applied", "failed", "withheld", "refused", "parked"}
    return TechLeadCharterDecision(
        decision_id=f"decision:run:{issue}", source=CharterDecisionSource.DECISION,
        run_id="run", action_id="A1", anchor_issue_number=ANCHOR, target_number=issue,
        target_is_pr=False, action_kind="escalate_to_human" if executed else "propose_decision",
        role=CharterRole.FLOW, required_depth=CharterDepth.WORKAROUND,
        binding=CharterBinding.FLOOR if executed else CharterBinding.OPERATOR_DECISION,
        role_enabled=True, role_depth=CharterDepth.RESTRUCTURE,
        role_authority=CharterAuthority.EXECUTE, action_ceiling=CharterAuthority.EXECUTE,
        ceiling_source="c",
        outcome=CharterOutcome.EXECUTED if executed else CharterOutcome.PROPOSED,
        reason_code=(
            CharterReason.FLOOR_ALWAYS_EXECUTES if executed
            else CharterReason.OPERATOR_DECISION_ALWAYS_PROPOSED
        ),
        reason="r", decided_at="2026-10-02T12:00:00+00:00",
        execution=CharterExecutionResult(effect) if executed else None,
        lifecycle=None if executed else CharterProposalLifecycle(effect),
        proposal_issue_number=None if executed or not filed else 950,
        triage_class=triage_class, triage_fingerprint=fingerprint,
    )


_NO_OPEN_PROPOSALS = OpenProposals(by_source={}, numbers=frozenset())


def _filed_by(source: tuple[str, str], proposal: int) -> OpenProposals:
    return OpenProposals(by_source={source: proposal}, numbers=frozenset({proposal}))


@dataclass
class _Authority:
    """The two reads ``triage_owed`` makes of the authority store."""

    charter_ledger: _Ledger
    ops: tuple = ()

    def list_ops(self):
        return self.ops


#: The needs-human episode every issue is in, unless a test says otherwise.
EP = "2026-10-02T09:00:00+00:00#1"


def _onset(label: str, event_id: int = 1) -> str:
    """A non-needs-human blocking label's part of a block episode (#8731)."""
    return f"{label}={_application_event(event_id).created_at}#{event_id}"


@dataclass
class _Episodes:
    """In-memory needs-human generations (#8688), bound as the store binds them."""

    recorded: dict[int, str] = field(default_factory=dict)
    bound: dict[int, int] = field(default_factory=dict)
    busy: set[int] = field(default_factory=set)

    @contextmanager
    def mutate_needs_human(self, issue_number):
        yield (
            IssueDispositionGateStatus.BUSY if issue_number in self.busy
            else IssueDispositionGateStatus.ACQUIRED
        )

    def needs_human_episodes(self, issue_numbers):
        return {n: self.recorded[n] for n in issue_numbers if n in self.recorded}

    def bind_needs_human_episode(self, issue_number, *, event_id, applied_at):
        if issue_number not in self.recorded or self.bound.get(issue_number, event_id) != event_id:
            self.recorded[issue_number] = f"{applied_at}#gh{event_id}"
        self.bound[issue_number] = event_id
        return self.recorded[issue_number]


def _rendered(episodes: Mapping[int, BlockEpisode]) -> dict[int, str]:
    """The episodes as a fingerprint carries them."""
    return {number: str(episode) for number, episode in episodes.items()}


def _application_event(event_id: int) -> LabelEvent:
    return LabelEvent(
        event_id=event_id, actor_login="operator", actor_is_bot=False,
        created_at=f"2026-10-0{event_id % 9 + 1}T00:00:00Z", actor_id=7,
    )


def _episode_owner(
    store: _Episodes,
    applications: dict[Any, Any] | None = None,
    *,
    clock: Callable[[], float] = lambda: 0.0,
    reads: list[int] | None = None,
) -> BlockEpisodes:
    """The block-episode owner over *store*, GitHub's applications faked;
    *reads* records each event read it makes (one per item per read)."""

    def standing_all(number: int, labels: Sequence[str]) -> dict[str, LabelEvent | None]:
        if reads is not None:
            reads.append(number)
        return {label: _application(applications, number, label) for label in labels}

    return BlockEpisodes(
        needs_human=NeedsHumanEpisodes(
            store=store, label_applications=standing_all, labels=LabelManager(_config()),
        ),
        label_applications=standing_all, labels=LabelManager(_config()),
        clock=clock, recheck_seconds=lambda: 3600,
    )


def _owner(
    issues: list[Issue],
    *,
    episodes: _Episodes | None = None,
    applications: dict[int, Any] | None = None,
    causes: dict[int, frozenset[NeedsHumanCause]] | None = None,
    ledger: _Ledger | None = None,
    timeline: dict[int, list[TimelineRecord]] | None = None,
    open_proposals: OpenProposals | None = None,
    rulings: dict[int, tuple[StandingRuling, ...]] | None = None,
) -> StateBlockedItemTriage:
    state = OrchestratorState()
    state.cached_scope_issues = issues
    config = _config()
    return StateBlockedItemTriage(
        config=config,
        state=lambda: state,
        labels=LabelManager(config),
        needs_human_causes=lambda numbers: {n: (causes or {}).get(n, frozenset()) for n in numbers},
        charter_ledger=ledger or _Ledger(),
        open_proposals=lambda: open_proposals or _NO_OPEN_PROPOSALS,
        timeline_reader=lambda number, limit: (timeline or {}).get(number, []),
        standing_rulings=lambda number: (rulings or {}).get(number, ()),
        episodes=_episode_owner(
            episodes if episodes is not None else _Episodes({i.number: EP for i in issues}),
            applications,
        ),
    )


def _application(applications: dict[Any, Any] | None, number: int, label: str) -> LabelEvent | None:
    """GitHub's standing application of the label: event 1 unless a test says
    otherwise, by ``(issue, label)`` or for every label of the issue (None:
    not standing; an exception: the read failed)."""
    given = applications or {}
    found = given.get((number, label.casefold()), given.get(number, 1))
    if isinstance(found, Exception):
        raise found
    return None if found is None else _application_event(found)


def _porchpin_board() -> list[Issue]:
    """Porchpin's four blocked items on 2026-10-02, plus things that are not."""
    return [
        _issue(179, "agent:backend", "needs-human", "tech-lead-needs-human"),
        _issue(262, "agent:backend", "needs-human", title="Live seller pickup index"),
        _issue(326, "agent:backend", "needs-human", "blocked-cross-milestone"),
        _issue(364, "agent:backend", "needs-human", "pr-pending"),
        _issue(400, "agent:backend"),  # runnable, not blocked
        _issue(410, "agent:tech-lead", "needs-human"),  # tech-lead machinery
        _issue(411, "agent:tech-lead", AWAITING_APPROVAL_LABEL),  # a proposal
        _issue(ANCHOR, "agent:tech-lead", "needs-human"),  # this review's anchor
    ]


# -- the agenda ----------------------------------------------------------------


def test_every_blocked_work_item_is_on_the_agenda_with_its_facts() -> None:
    owner = _owner(
        _porchpin_board(),
        causes={262: frozenset({NeedsHumanCause.AGENT_COMPLETION, NeedsHumanCause.SESSION_LIFECYCLE})},
        timeline={262: [TimelineRecord(
            event_id="e1", timestamp="2026-09-23T06:38:06Z", event="issue.needs_human",
            data={"question": SPLIT_QUESTION, "reason": "Agent requested human input"},
        )]},
    )

    agenda = owner.agenda(anchor_issue_number=ANCHOR)

    assert [item.issue_number for item in agenda.items] == [179, 262, 326, 364]
    by_number = {item.issue_number: item for item in agenda.items}
    assert by_number[262].agent_question == SPLIT_QUESTION
    assert by_number[262].needs_human_causes == ("agent_completion", "session_lifecycle")
    assert by_number[262].reason == "never triaged"
    # The tech lead's own hand-over marker does not count as a block change.
    assert by_number[179].fingerprint == f"@{EP}"
    assert by_number[326].fingerprint == (
        f"blocked-cross-milestone,needs-human@{EP};{_onset('blocked-cross-milestone')}"
    )
    assert agenda.grants == tuple(TriageGrant(i.issue_number, i.fingerprint) for i in agenda.items)


@pytest.mark.parametrize(
    ("effect", "in_force"),
    [
        ("awaiting_approval", True),
        ("declined", True),  # the operator answered: theirs now
        ("approved_applied", True),
        ("approved_stale", False),
        ("applied", True),
        ("failed", False),
        ("withheld", False),
        ("parked", False),
    ],
)
def test_an_unchanged_item_is_not_triaged_again_while_its_triage_is_in_force(
    effect: str, in_force: bool
) -> None:
    triage_class = (
        TriageClass.HUMAN_HAND_OVER
        if effect in {"applied", "failed", "withheld", "parked"}
        else TriageClass.OPERATOR_DECISION
    )
    ledger = _Ledger({262: [_triage_record(262, triage_class, f"needs-human@{EP}", effect=effect)]})
    owner = _owner(
        [_issue(262, "agent:backend", "needs-human")], ledger=ledger,
        open_proposals=_filed_by(("run", "A1"), 950) if effect == "awaiting_approval" else None,
    )

    agenda = owner.agenda(anchor_issue_number=ANCHOR)

    assert (agenda.in_force == (262,)) is in_force
    assert ([item.issue_number for item in agenda.items] == [262]) is (not in_force)


def test_a_proposal_that_never_got_filed_is_not_a_triage_in_force() -> None:
    """Awaiting approval only counts once the proposal issue exists: a creation
    that failed left the record routed to the gate with nothing to approve."""
    ledger = _Ledger({262: [_triage_record(
        262, TriageClass.OPERATOR_DECISION, f"needs-human@{EP}", effect="awaiting_approval", filed=False,
    )]})
    owner = _owner([_issue(262, "agent:backend", "needs-human")], ledger=ledger)

    [item] = owner.agenda(anchor_issue_number=ANCHOR).items

    assert item.reason.endswith("did not take effect: awaiting_approval")


@pytest.mark.parametrize("linked", [True, False])
def test_a_filed_proposal_is_in_force_by_its_open_op_whatever_the_link(linked: bool) -> None:
    """#7593 review r4: the proposal was filed and its op recorded, but linking
    its number onto the charter record failed (or did not). The open op this
    decision filed is the durable fact: no second health review triages it."""
    ledger = _Ledger({262: [_triage_record(
        262, TriageClass.OPERATOR_DECISION, f"needs-human@{EP}", effect="awaiting_approval", filed=linked,
    )]})
    owner = _owner(
        [_issue(262, "agent:backend", "needs-human")], ledger=ledger,
        open_proposals=_filed_by(("run", "A1"), 950),
    )

    agenda = owner.agenda(anchor_issue_number=ANCHOR)

    assert agenda.in_force == (262,) and agenda.items == ()


def test_a_reused_proposal_is_in_force_by_its_link_while_it_is_open() -> None:
    """A re-proposal that joined an open proposal by reuse names it on its
    record; it is in force while that proposal is open."""
    ledger = _Ledger({262: [_triage_record(
        262, TriageClass.OPERATOR_DECISION, f"needs-human@{EP}", effect="awaiting_approval",
    )]})
    owner = _owner(
        [_issue(262, "agent:backend", "needs-human")], ledger=ledger,
        open_proposals=_filed_by(("older-run", "A4"), 950),
    )

    assert owner.agenda(anchor_issue_number=ANCHOR).in_force == (262,)


def test_an_older_proposal_never_stands_in_for_a_newer_decision_that_was_not_filed() -> None:
    """#7593 review r6: an older proposal for #262 is still open; the block
    changed, the tech lead proposed a different decision, and filing it
    failed. The newer decision has no proposal: #262 is owed a triage."""
    ledger = _Ledger({262: [_triage_record(
        262, TriageClass.OPERATOR_DECISION, f"blocked-failed,needs-human@{EP};{_onset('blocked-failed')}",
        effect="awaiting_approval", filed=False,
    )]})
    owner = _owner(
        [_issue(262, "agent:backend", "needs-human", "blocked-failed")], ledger=ledger,
        open_proposals=_filed_by(("older-run", "A4"), 940),
    )

    [item] = owner.agenda(anchor_issue_number=ANCHOR).items

    assert item.issue_number == 262
    assert item.prior is not None and item.prior.proposal_issue_number is None


def test_a_linked_proposal_whose_op_is_gone_is_not_in_force() -> None:
    """The record still says awaiting approval, but no proposal is open for
    the item any more: nothing waits on the operator, so it is owed again."""
    ledger = _Ledger({262: [_triage_record(
        262, TriageClass.OPERATOR_DECISION, f"needs-human@{EP}", effect="awaiting_approval",
    )]})
    owner = _owner([_issue(262, "agent:backend", "needs-human")], ledger=ledger)

    [item] = owner.agenda(anchor_issue_number=ANCHOR).items

    assert item.issue_number == 262 and item.prior is not None
    assert item.prior.proposal_issue_number is None


def test_a_changed_block_is_triaged_again() -> None:
    ledger = _Ledger({326: [_triage_record(
        326, TriageClass.HUMAN_HAND_OVER, f"blocked-cross-milestone,needs-human@{EP};{_onset('blocked-cross-milestone')}", effect="applied",
    )]})
    # The engine fix took the stale dependency label off (#7333).
    owner = _owner([_issue(326, "agent:backend", "needs-human")], ledger=ledger)

    [item] = owner.agenda(anchor_issue_number=ANCHOR).items

    assert item.issue_number == 326
    assert item.reason.startswith("its block changed since it was triaged human_hand_over")
    assert item.prior is not None and item.prior.effect == "applied"


# -- the tech lead's own hand-over (#8112) -------------------------------------

#: The needs-human generation a hand-over opens when it puts the label on.
HANDED_OVER = "2026-10-04T13:30:43+00:00#2"


def _handed_over(
    blocked: tuple[str, ...], after: tuple[str, ...], store: _Episodes,
    applications: dict[Any, Any] | None = None,
) -> tuple[Any, bool]:
    """Triage the item human_hand_over on the block it is granted with
    *blocked*, let the escalation leave it with *after*, and return the next
    health review's agenda and whether the tick owes a triage."""
    from issue_orchestrator.control.blocked_item_triage import triage_owed

    [granted] = _owner([_issue(8091, "agent:backend", *blocked)]).agenda(
        anchor_issue_number=ANCHOR,
    ).grants
    ledger = _Ledger({8091: [_triage_record(
        8091, TriageClass.HUMAN_HAND_OVER, granted.fingerprint, effect="applied",
    )]})
    issue = _issue(8091, "agent:backend", *after)
    state = OrchestratorState()
    state.cached_scope_issues = [issue]
    owed = triage_owed(_config(), state, _Authority(ledger), _episode_owner(store, applications))
    agenda = _owner([issue], ledger=ledger, episodes=store, applications=applications).agenda(
        anchor_issue_number=ANCHOR,
    )
    return agenda, owed


@pytest.mark.parametrize(
    ("blocked", "after", "generation"),
    [
        # #8091: the agent's needs-human block; the escalation adds the marker.
        (("needs-human",), ("needs-human", "tech-lead-needs-human"), EP),
        # A block without needs-human: the escalation puts the marker and
        # needs-human on, opening a needs-human generation of its own.
        (("blocked-failed",), ("blocked-failed", "needs-human", "tech-lead-needs-human"), HANDED_OVER),
    ],
)
def test_a_hand_over_does_not_regrant_its_own_item(
    blocked: tuple[str, ...], after: tuple[str, ...], generation: str,
) -> None:
    """#8112: health review #8095 triaged #8091 human_hand_over on its
    needs-human block; the escalation then put the tech-lead hand-over marker
    on. The block did not change, but the next review was granted #8091 again
    ("its block changed ... (needs-human -> none)") and pushed to hand it over
    a second time. The hand-over's own marker, and the needs-human block it
    places, are not a block change."""
    agenda, owed = _handed_over(blocked, after, _Episodes({8091: generation}))

    assert agenda.items == () and agenda.in_force == (8091,)
    assert owed is False


@pytest.mark.parametrize(
    ("blocked", "after", "generation", "applications"),
    [
        # A new block beside the hand-over.
        (("needs-human",), ("blocked-failed", "needs-human", "tech-lead-needs-human"), EP, None),
        # needs-human lifted and raised again under the marker: a new episode.
        (("needs-human",), ("needs-human", "tech-lead-needs-human"), HANDED_OVER, None),
        # The handed-over item's own block re-raised: a new episode.
        (
            ("blocked-failed",), ("blocked-failed", "needs-human", "tech-lead-needs-human"),
            HANDED_OVER, {(8091, "blocked-failed"): 2},
        ),
        # The block the triage was decided on was lifted under the hand-over.
        (("blocked-failed", "needs-human"), ("needs-human", "tech-lead-needs-human"), EP, None),
    ],
)
def test_a_handed_over_item_whose_block_changed_is_triaged_again(
    blocked: tuple[str, ...], after: tuple[str, ...], generation: str,
    applications: dict[Any, Any] | None,
) -> None:
    """#8112: only the hand-over itself is exempt. A block that changed while
    the marker is on is owed a triage like any other."""
    agenda, owed = _handed_over(blocked, after, _Episodes({8091: generation}), applications)

    assert [item.issue_number for item in agenda.items] == [8091] and agenda.in_force == ()
    assert owed is True


def test_a_triage_does_not_cover_a_later_episode_of_the_same_block() -> None:
    """#8688 (porchpin #450): a triage of one needs-human block stayed "in
    force" after the block was lifted (the operator approved its proposal) and
    the engine re-blocked the item under the same label for a new agent
    question. A triage covers the block EPISODE it was decided on, not every
    later block that happens to carry the same labels."""
    from issue_orchestrator.control.blocked_item_triage import triage_owed

    first, second = "2026-10-04T11:55:04+00:00#7", "2026-10-08T00:54:00+00:00#9"
    ledger = _Ledger({450: [_triage_record(
        450, TriageClass.EXPLAINED, f"needs-human@{first}", effect="applied",
    )]})
    issue = _issue(450, "agent:backend", "needs-human")
    episodes = _Episodes({450: first})
    assert _owner([issue], ledger=ledger, episodes=episodes).agenda(
        anchor_issue_number=ANCHOR,
    ).in_force == (450,)

    episodes.recorded[450] = second  # lifted, then blocked again
    agenda = _owner([issue], ledger=ledger, episodes=episodes).agenda(anchor_issue_number=ANCHOR)

    [item] = agenda.items
    assert (item.issue_number, item.fingerprint) == (450, f"needs-human@{second}")
    assert item.reason.startswith("it was blocked again, under the same labels")
    assert agenda.in_force == ()
    state = OrchestratorState()
    state.cached_scope_issues = [issue]
    assert triage_owed(_config(), state, _Authority(ledger), _episode_owner(episodes)) is True


def test_a_block_whose_episode_is_unrecorded_is_owed_then_bound_at_its_grant() -> None:
    """#8688/#8697: with no recorded generation (a hand-placed label, or one
    older than the records) a re-block cannot be told from the triaged block,
    so the item is owed a triage. The grant binds an episode to GitHub's
    standing application of the label, dated by it, and the triage made on it
    covers the item while that application stands."""
    from issue_orchestrator.control.blocked_item_triage import triage_owed

    issue = _issue(450, "agent:backend", "needs-human")
    state = OrchestratorState()
    state.cached_scope_issues = [issue]
    episodes = _Episodes()
    old = _Ledger({450: [_triage_record(450, TriageClass.EXPLAINED, "needs-human", effect="applied")]})
    assert triage_owed(_config(), state, _Authority(old), _episode_owner(episodes, {450: 41})) is True

    [item] = _owner(
        [issue], ledger=old, episodes=episodes, applications={450: 41},
    ).agenda(anchor_issue_number=ANCHOR).items

    assert item.fingerprint == f"needs-human@{_application_event(41).created_at}#gh41"
    triaged = _Ledger({450: [_triage_record(450, TriageClass.EXPLAINED, item.fingerprint, effect="applied")]})
    assert triage_owed(
        _config(), state, _Authority(triaged), _episode_owner(episodes, {450: 41}),
    ) is False


def test_a_label_reapplied_outside_the_owner_is_a_new_episode() -> None:
    """#8688 review r1: the operator removes needs-human and puts it back by
    hand between the owner's observations. The owner's generation survives,
    but GitHub's standing application of the label is a new event: the next
    agenda opens a new episode and grants the item."""
    issue = _issue(450, "agent:backend", "needs-human")
    episodes = _Episodes({450: EP})
    first = _owner([issue], episodes=episodes, applications={450: 7}).agenda(anchor_issue_number=ANCHOR)
    [granted] = first.grants
    ledger = _Ledger({450: [_triage_record(450, TriageClass.EXPLAINED, granted.fingerprint, effect="applied")]})
    assert _owner(
        [issue], ledger=ledger, episodes=episodes, applications={450: 7},
    ).agenda(anchor_issue_number=ANCHOR).in_force == (450,)

    agenda = _owner(
        [issue], ledger=ledger, episodes=episodes, applications={450: 8},
    ).agenda(anchor_issue_number=ANCHOR)

    [item] = agenda.items
    assert item.fingerprint != granted.fingerprint and agenda.in_force == ()
    assert item.reason.startswith("it was blocked again, under the same labels")


def test_a_hand_reapplication_makes_a_review_due_at_the_next_recheck() -> None:
    """#8688 review r2 F1: the board and the recorded episode are unchanged
    after a hand re-application, so only GitHub can tell. The tick's check
    reads no events between rechecks, and at the recheck (once per review
    interval) finds the new application: a review is due on the same board."""
    from issue_orchestrator.control.blocked_item_triage import triage_owed
    from issue_orchestrator.control.health_review_trigger import health_review_decision

    config = _config()
    config.tech_lead.health_review.interval_minutes = 60
    issue = _issue(450, "agent:backend", "needs-human")
    state = OrchestratorState()
    state.cached_scope_issues = [issue]
    store, now, reads = _Episodes({450: EP}), [0.0], []
    applications = {450: 7}
    episodes = _episode_owner(store, applications, clock=lambda: now[0], reads=reads)
    [granted] = _owner([issue], episodes=store, applications=applications).agenda(
        anchor_issue_number=ANCHOR,
    ).grants
    authority = _Authority(_Ledger({450: [_triage_record(
        450, TriageClass.EXPLAINED, granted.fingerprint, effect="applied",
    )]}))
    assert triage_owed(config, state, authority, episodes) is False and reads == [450]
    reviewed = health_review_decision(config, state, 0.0).fingerprint
    state.last_reviewed_board_fingerprint = reviewed

    applications[450] = 8  # taken off and put back by hand, unseen by the owner
    now[0] = 1800.0
    assert triage_owed(config, state, authority, episodes) is False
    assert reads == [450]  # no GitHub read between rechecks

    now[0] = 3600.0
    decision = health_review_decision(
        config, state, 1000.0 + 3600 * 24,
        triage_owed=lambda: triage_owed(config, state, authority, episodes),
    )
    assert decision.fingerprint == reviewed and decision.due is True
    assert reads == [450, 450]


def test_a_binding_waits_for_the_block_owners_gate() -> None:
    """#8688 review r2 F2: while the block's owner holds its per-issue gate it
    may be opening a new generation, so no GitHub event read then is bound to
    it: the episode is unknown, and the item owed."""
    issue = _issue(450, "agent:backend", "needs-human")
    store = _Episodes({450: EP}, busy={450})
    ledger = _Ledger({450: [_triage_record(450, TriageClass.EXPLAINED, f"needs-human@{EP}", effect="applied")]})
    reads: list[int] = []

    agenda = _owner([issue], ledger=ledger, episodes=store).agenda(anchor_issue_number=ANCHOR)
    assert _episode_owner(store, reads=reads).verified({450: issue}) == {}

    [item] = agenda.items
    assert item.fingerprint == "needs-human@unknown" and store.bound == {} and reads == []


def test_every_recheck_reads_github_whatever_the_cached_snapshot_says() -> None:
    """#8688 review r6 F1: the cached issue snapshot can be hours old (an
    incremental refresh that skips it), so an unchanged snapshot proves
    nothing: each verification reads the standing application again."""
    store, reads, applications = _Episodes({450: EP}), [], {450: 7}
    # Long after the snapshot's updated_at: nothing about its age is suspect.
    episodes = _episode_owner(store, applications, reads=reads, clock=lambda: 4e9)
    issue = Issue(
        number=450, title="t", labels=["needs-human"], repo="porchpin/porchpin", state="open",
        updated_at="2026-10-08T00:00:00Z",
    )

    first = episodes.verified({450: issue})
    applications[450] = 8  # re-applied by hand; the snapshot still says 00:00:00

    assert episodes.verified({450: issue}) != first and reads == [450, 450]


def test_an_item_a_recheck_could_not_verify_stays_owed_until_one_does() -> None:
    """#8688 review r5 F1: the recheck after a hand re-application fails to
    read GitHub. The item must stay owed on every tick until a later recheck
    verifies it, not only on the tick of the failed read."""
    from issue_orchestrator.control.blocked_item_triage import triage_owed

    issue = _issue(450, "agent:backend", "needs-human")
    state = OrchestratorState()
    state.cached_scope_issues = [issue]
    store, now, applications = _Episodes({450: EP}), [0.0], {450: 7}
    episodes = _episode_owner(store, applications, clock=lambda: now[0])
    [granted] = _owner([issue], episodes=store, applications=applications).agenda(
        anchor_issue_number=ANCHOR,
    ).grants
    authority = _Authority(_Ledger({450: [_triage_record(
        450, TriageClass.EXPLAINED, granted.fingerprint, effect="applied",
    )]}))
    assert triage_owed(_config(), state, authority, episodes) is False

    applications[450] = RuntimeError("events API 502")  # re-applied by hand; the read fails
    now[0] = 3600.0
    assert triage_owed(_config(), state, authority, episodes) is True
    now[0] = 3660.0
    assert triage_owed(_config(), state, authority, episodes) is True  # still owed between rechecks

    applications[450] = 7  # GitHub reads again: the same application after all
    now[0] = 7200.0
    assert triage_owed(_config(), state, authority, episodes) is False


def test_a_verified_item_still_waits_for_the_owners_gate() -> None:
    """#8688 review r5 F2: every verification, including one of an item
    verified before, waits for the owner's gate; while the owner is changing
    the block the episode is unknown."""
    store = _Episodes({450: EP})
    episodes = _episode_owner(store, {450: 7})
    issue = Issue(
        number=450, title="t", labels=["needs-human"], repo="porchpin/porchpin", state="open",
        updated_at="2026-10-08T00:00:00Z",
    )
    assert _rendered(episodes.verified({450: issue})) == {450: EP}

    store.busy.add(450)

    assert episodes.verified({450: issue}) == {}


@pytest.mark.parametrize("application", [None, RuntimeError("events API 502")])
def test_an_unverifiable_episode_is_owed_and_never_covered(application: Any) -> None:
    """GitHub does not show the label standing (a stale cache), or its events
    cannot be read completely: the episode is unknown, so a triage in force on
    the recorded episode does not cover it, and nothing is bound."""
    issue = _issue(450, "agent:backend", "needs-human")
    episodes = _Episodes({450: EP})
    ledger = _Ledger({450: [_triage_record(450, TriageClass.EXPLAINED, f"needs-human@{EP}", effect="applied")]})

    [item] = _owner(
        [issue], ledger=ledger, episodes=episodes, applications={450: application},
    ).agenda(anchor_issue_number=ANCHOR).items

    assert item.fingerprint == "needs-human@unknown"
    assert item.reason.startswith("its block's onset is not recorded or could not be verified")
    assert episodes.bound == {}
    recorded = _Ledger({450: [_triage_record(450, TriageClass.EXPLAINED, item.fingerprint, effect="applied")]})
    assert _owner(
        [issue], ledger=recorded, episodes=episodes, applications={450: application},
    ).agenda(anchor_issue_number=ANCHOR).in_force == ()


@pytest.mark.parametrize("label", ["publish-failed", "blocked-failed", "blocked:claim-lost", "blocked"])
def test_a_block_without_needs_human_reraised_alone_is_a_new_episode(label: str) -> None:
    """#8731: a block of an engine-written label with no needs-human beside it
    (``publish-failed`` after a failed publish) is triaged; a retry succeeds
    and lifts it; the publish fails again and the label comes back alone. The
    labels are the same, but GitHub's standing application of the label is a
    new event: the old triage does not cover the new block."""
    issue = _issue(500, "agent:backend", label)
    first = _owner([issue], applications={500: 7}).agenda(anchor_issue_number=ANCHOR)
    [granted] = first.grants
    assert granted.fingerprint == f"{label}@{_onset(label, 7)}"
    ledger = _Ledger({500: [_triage_record(500, TriageClass.EXPLAINED, granted.fingerprint, effect="applied")]})
    assert _owner(
        [issue], ledger=ledger, applications={500: 7},
    ).agenda(anchor_issue_number=ANCHOR).in_force == (500,)

    agenda = _owner([issue], ledger=ledger, applications={500: 8}).agenda(anchor_issue_number=ANCHOR)

    [item] = agenda.items
    assert agenda.in_force == () and item.fingerprint == f"{label}@{_onset(label, 8)}"
    assert item.reason.startswith("it was blocked again, under the same labels")


def test_a_block_without_needs_human_is_not_churned_while_its_label_stands() -> None:
    """#8731: the onset of a label with no single writer is GitHub's standing
    application of it, stable for as long as the label stays on, so a triage
    in force is not re-granted on every health review."""
    issue = _issue(500, "agent:backend", "blocked-failed", "recovery-pending")
    [granted] = _owner([issue], applications={500: 3}).agenda(anchor_issue_number=ANCHOR).grants
    ledger = _Ledger({500: [_triage_record(500, TriageClass.EXPLAINED, granted.fingerprint, effect="applied")]})

    for _review in range(3):
        agenda = _owner([issue], ledger=ledger, applications={500: 3}).agenda(anchor_issue_number=ANCHOR)
        assert agenda.in_force == (500,) and agenda.items == ()


def test_a_label_triaged_before_its_onset_was_recorded_is_owed_once() -> None:
    """#8731 deploy: a triage recorded on the labels alone (before onsets were
    recorded) cannot be shown to cover the block standing now, so the item is
    owed one triage, and the triage made on its onset then covers it."""
    issue = _issue(500, "agent:backend", "blocked-failed")
    legacy = _Ledger({500: [_triage_record(500, TriageClass.EXPLAINED, "blocked-failed", effect="applied")]})

    [item] = _owner([issue], ledger=legacy).agenda(anchor_issue_number=ANCHOR).items

    triaged = _Ledger({500: [_triage_record(500, TriageClass.EXPLAINED, item.fingerprint, effect="applied")]})
    assert _owner([issue], ledger=triaged).agenda(anchor_issue_number=ANCHOR).in_force == (500,)


def test_reraising_one_label_of_a_mixed_block_is_a_new_episode() -> None:
    """#8731: ``needs-human`` stays on throughout (its episode unchanged) while
    ``blocked-failed`` beside it is lifted by a retry and re-raised by the next
    failure. The new failure is a new episode of the block."""
    issue = _issue(500, "agent:backend", "needs-human", "blocked-failed")
    store = _Episodes({500: EP})
    standing = {(500, "needs-human"): 4, (500, "blocked-failed"): 5}
    [granted] = _owner([issue], episodes=store, applications=standing).agenda(
        anchor_issue_number=ANCHOR,
    ).grants
    assert granted.fingerprint == f"blocked-failed,needs-human@{EP};{_onset('blocked-failed', 5)}"
    ledger = _Ledger({500: [_triage_record(500, TriageClass.EXPLAINED, granted.fingerprint, effect="applied")]})

    standing[(500, "blocked-failed")] = 6
    agenda = _owner([issue], ledger=ledger, episodes=store, applications=standing).agenda(
        anchor_issue_number=ANCHOR,
    )

    [item] = agenda.items
    assert item.fingerprint == f"blocked-failed,needs-human@{EP};{_onset('blocked-failed', 6)}"
    assert agenda.in_force == () and store.recorded == {500: EP}


@pytest.mark.parametrize("application", [None, RuntimeError("events API 502")])
def test_an_unverifiable_label_onset_is_owed_and_never_covered(application: Any) -> None:
    """#8731: GitHub does not show ``publish-failed`` standing (a stale cache),
    or the events cannot be read completely: the block's episode is unknown,
    owed, and never covered, even beside a verified needs-human episode."""
    for labels in (("publish-failed",), ("needs-human", "publish-failed")):
        issue = _issue(500, "agent:backend", *labels)
        standing = {(500, "needs-human"): 1, (500, "publish-failed"): application}

        [item] = _owner([issue], applications=standing).agenda(anchor_issue_number=ANCHOR).items

        assert item.fingerprint == f"{','.join(sorted(labels))}@unknown"
        recorded = _Ledger({500: [_triage_record(500, TriageClass.EXPLAINED, item.fingerprint, effect="applied")]})
        assert _owner(
            [issue], ledger=recorded, applications=standing,
        ).agenda(anchor_issue_number=ANCHOR).in_force == ()


def test_one_event_read_dates_every_blocking_label_of_an_item() -> None:
    """#8731: an item blocked by several labels is dated by ONE read of its
    events, not one per label, and none is made between the tick's rechecks."""
    from issue_orchestrator.control.blocked_item_triage import triage_owed

    issue = _issue(500, "agent:backend", "blocked", "blocked-failed", "publish-failed")
    state = OrchestratorState()
    state.cached_scope_issues = [issue]
    now, reads = [0.0], []
    episodes = _episode_owner(_Episodes(), clock=lambda: now[0], reads=reads)
    [granted] = _owner([issue]).agenda(anchor_issue_number=ANCHOR).grants
    authority = _Authority(_Ledger({500: [_triage_record(
        500, TriageClass.EXPLAINED, granted.fingerprint, effect="applied",
    )]}))

    assert triage_owed(_config(), state, authority, episodes) is False and reads == [500]
    now[0] = 1800.0
    assert triage_owed(_config(), state, authority, episodes) is False and reads == [500]


def test_the_ticks_recheck_finds_a_reraised_label_on_an_unchanged_board() -> None:
    """#8731: ``publish-failed`` is lifted and re-raised between two ticks; the
    board (labels only) is unchanged. The tick's recheck, once per interval,
    finds the new application, and a review is due. An item the recheck
    cannot verify stays owed until one does."""
    from issue_orchestrator.control.blocked_item_triage import triage_owed
    from issue_orchestrator.control.health_review_trigger import health_review_decision

    config = _config()
    config.tech_lead.health_review.interval_minutes = 60
    issue = _issue(500, "agent:backend", "publish-failed")
    state = OrchestratorState()
    state.cached_scope_issues = [issue]
    now, applications = [0.0], {500: 7}
    episodes = _episode_owner(_Episodes(), applications, clock=lambda: now[0])
    [granted] = _owner([issue], applications=applications).agenda(anchor_issue_number=ANCHOR).grants
    authority = _Authority(_Ledger({500: [_triage_record(
        500, TriageClass.EXPLAINED, granted.fingerprint, effect="applied",
    )]}))
    assert triage_owed(config, state, authority, episodes) is False
    reviewed = health_review_decision(config, state, 0.0).fingerprint
    state.last_reviewed_board_fingerprint = reviewed

    applications[500] = 8  # the retry lifted it; the next publish failed again
    now[0] = 1800.0
    assert triage_owed(config, state, authority, episodes) is False  # no read between rechecks
    now[0] = 3600.0
    decision = health_review_decision(
        config, state, 1000.0 + 3600 * 24,
        triage_owed=lambda: triage_owed(config, state, authority, episodes),
    )
    assert decision.fingerprint == reviewed and decision.due is True

    applications[500] = RuntimeError("events API 502")
    now[0] = 7200.0
    assert triage_owed(config, state, authority, episodes) is True
    applications[500] = 7  # readable again, and the triaged application after all
    now[0] = 9000.0
    assert triage_owed(config, state, authority, episodes) is True  # still owed between rechecks
    now[0] = 10800.0
    assert triage_owed(config, state, authority, episodes) is False


def test_a_label_added_between_rechecks_is_read_at_once() -> None:
    """#8731: between rechecks the tick has no proven onset for a blocking
    label put on since; it reads that item's events at once (one scan) rather
    than covering it with a triage of an older block."""
    issue = _issue(500, "agent:backend", "blocked-failed")
    now, reads, applications = [0.0], [], {(500, "blocked-failed"): 1, (500, "publish-failed"): 2}
    episodes = _episode_owner(_Episodes(), applications, clock=lambda: now[0], reads=reads)
    assert _rendered(episodes.current({500: issue})) == {500: _onset("blocked-failed")}

    now[0] = 60.0
    grown = _issue(500, "agent:backend", "blocked-failed", "publish-failed")
    assert _rendered(episodes.current({500: grown})) == {
        500: f"{_onset('blocked-failed')};{_onset('publish-failed', 2)}",
    }
    assert reads == [500, 500]
    now[0] = 120.0
    assert episodes.current({500: grown}) and reads == [500, 500]  # proven: no further read


def test_a_lift_and_reraise_the_tick_sees_is_owed_before_the_next_recheck() -> None:
    """#8731 review r1 F1: the tick's snapshot shows ``publish-failed`` lifted
    (by a successful retry), and a later snapshot shows it back, all before
    the next recheck. The onset proven before the lift is forgotten when the
    lift is seen, so the re-raise is read afresh and owed at once."""
    from issue_orchestrator.control.blocked_item_triage import triage_owed

    blocked = _issue(500, "agent:backend", "publish-failed")
    state = OrchestratorState()
    state.cached_scope_issues = [blocked]
    now, applications = [0.0], {500: 7}
    episodes = _episode_owner(_Episodes(), applications, clock=lambda: now[0])
    [granted] = _owner([blocked], applications=applications).agenda(anchor_issue_number=ANCHOR).grants
    authority = _Authority(_Ledger({500: [_triage_record(
        500, TriageClass.EXPLAINED, granted.fingerprint, effect="applied",
    )]}))
    assert triage_owed(_config(), state, authority, episodes) is False

    now[0] = 600.0
    state.cached_scope_issues = [_issue(500, "agent:backend")]  # the retry lifted it
    assert triage_owed(_config(), state, authority, episodes) is False
    applications[500] = 8  # the next publish failed: put on again
    now[0] = 1200.0
    state.cached_scope_issues = [blocked]

    assert triage_owed(_config(), state, authority, episodes) is True


def test_needs_human_seen_lifted_and_put_back_by_hand_is_read_at_once() -> None:
    """#8731 review r2 F1: a mixed block's ``needs-human`` is taken off while
    ``blocked-failed`` stays (the tick sees it), then put back by hand, all
    before the next recheck. The owner never sees the hand write, so the
    re-application must be read from GitHub when the tick sees it come back."""
    from issue_orchestrator.control.blocked_item_triage import triage_owed

    mixed = _issue(500, "agent:backend", "needs-human", "blocked-failed")
    state = OrchestratorState()
    state.cached_scope_issues = [mixed]
    store, now, reads = _Episodes({500: EP}), [0.0], []
    applications: dict[Any, Any] = {(500, "needs-human"): 4, (500, "blocked-failed"): 5}
    episodes = _episode_owner(store, applications, clock=lambda: now[0], reads=reads)
    [granted] = _owner([mixed], episodes=store, applications=applications).agenda(
        anchor_issue_number=ANCHOR,
    ).grants
    authority = _Authority(_Ledger({500: [_triage_record(
        500, TriageClass.EXPLAINED, granted.fingerprint, effect="applied",
    )]}))
    assert triage_owed(_config(), state, authority, episodes) is False and reads == [500]

    now[0] = 600.0
    state.cached_scope_issues = [_issue(500, "agent:backend", "blocked-failed")]
    triage_owed(_config(), state, authority, episodes)
    applications[(500, "needs-human")] = 9  # put back by hand, unseen by the owner
    now[0] = 1200.0
    state.cached_scope_issues = [mixed]

    assert triage_owed(_config(), state, authority, episodes) is True
    assert reads == [500, 500]


def test_an_item_a_tick_read_could_not_verify_waits_for_the_next_recheck() -> None:
    """#8731: an unproven item whose read fails is owed, and is not re-read on
    every tick: it waits for the next recheck."""
    issue = _issue(500, "agent:backend", "blocked-failed")
    now, reads, applications = [0.0], [], {500: RuntimeError("events API 502")}
    episodes = _episode_owner(_Episodes(), applications, clock=lambda: now[0], reads=reads)
    assert episodes.current({}) == {}  # the first recheck: nothing blocked

    now[0] = 60.0
    assert episodes.current({500: issue}) == {} and reads == [500]
    now[0] = 120.0
    assert episodes.current({500: issue}) == {} and reads == [500]
    applications[500] = 1
    now[0] = 3600.0
    assert _rendered(episodes.current({500: issue})) == {500: _onset("blocked-failed")} and reads == [500, 500]


def test_a_mixed_block_is_verified_by_one_events_scan() -> None:
    """#8731 review r1 F2: needs-human and the labels beside it are dated by
    ONE read of the item's events, made under the needs-human owner's gate."""
    issue = _issue(500, "agent:backend", "needs-human", "blocked-failed", "publish-failed")
    reads: list[int] = []
    store = _Episodes({500: EP})

    episodes = _episode_owner(store, reads=reads).verified({500: issue})

    assert _rendered(episodes) == {500: f"{EP};{_onset('blocked-failed')};{_onset('publish-failed')}"}
    assert reads == [500] and store.bound == {500: 1}


def test_the_agenda_is_capped_per_run_oldest_first() -> None:
    issues = [_issue(n, "agent:backend", "blocked-failed") for n in range(100, 100 + MAX_TRIAGE_ITEMS_PER_RUN + 3)]

    agenda = _owner(issues).agenda(anchor_issue_number=ANCHOR)

    assert [item.issue_number for item in agenda.items] == list(range(100, 100 + MAX_TRIAGE_ITEMS_PER_RUN))
    assert len(agenda.deferred) == 3


def test_an_unreadable_ledger_fails_the_agenda_rather_than_guessing() -> None:
    class _Broken:
        def latest_triage_for_issue(self, issue_number: int):
            raise RuntimeError("authority store locked")

    owner = _owner([_issue(262, "agent:backend", "needs-human")], ledger=_Broken())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="locked"):
        owner.agenda(anchor_issue_number=ANCHOR)


def test_fingerprint_ignores_order_and_case() -> None:
    assert block_fingerprint(
        ["Needs-Human", "blocked-failed"], tech_lead_marker=False, needs_human_label="needs-human",
        episode=EP,
    ) == block_fingerprint(
        ["blocked-failed", "needs-human"], tech_lead_marker=False, needs_human_label="needs-human",
        episode=EP,
    )


# -- the completion rule -------------------------------------------------------


def _authority(*grants: TriageGrant) -> TechLeadLaunchAuthority:
    return TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW, anchor_issue_number=ANCHOR,
        triage_grants=tuple(grants),
    )


def _decide(*actions: ProposedTechLeadAction) -> TechLeadDecision:
    return TechLeadDecision(summary="walk the floor", proposed_actions=tuple(actions))


def _split(action_id: str = "A1", target: int = 262) -> ProposedTechLeadAction:
    return ProposedTechLeadAction(
        id=action_id, action_type="propose_decision", target_number=target,
        title="Split #262: land the share-page slice under Refs #262",
        body=f"The agent asked: {SPLIT_QUESTION}\n\nRecommend: split.",
        triage_class=TriageClass.OPERATOR_DECISION,
        follow_up_issues=(DecisionFollowUp(
            title="Live D1 seller pickup index", body="The remainder of #262's acceptance list.",
        ),),
    )


def _hand_over(action_id: str, target: int) -> ProposedTechLeadAction:
    return ProposedTechLeadAction(
        id=action_id, action_type="escalate_to_human", target_number=target,
        body="Cloudflare provisioning (D1, Access app, tokens) is the operator's.",
        triage_class=TriageClass.HUMAN_HAND_OVER,
    )


def test_a_decision_that_triages_every_granted_item_is_valid() -> None:
    authority = _authority(TriageGrant(179, ""), TriageGrant(262, "needs-human"))

    assert triage_coverage_violation(_decide(_split(), _hand_over("A2", 179)), authority) is None


def test_the_porchpin_shape_advice_on_the_anchor_alone_is_rejected() -> None:
    """What porchpin's tech lead did: diagnose, advise on its anchor, act on nothing."""
    advice = ProposedTechLeadAction(
        id="A1", action_type="post_comment", target_number=ANCHOR,
        body="#262 waits on the maintainer's split decision.",
    )

    violation = triage_coverage_violation(_decide(advice), _authority(TriageGrant(262, "needs-human")))

    assert violation is not None and "untriaged: #262" in violation


def test_one_item_two_triages_is_rejected() -> None:
    explain = ProposedTechLeadAction(
        id="A2", action_type="post_comment", target_number=262, body="Waiting on a split decision.",
        triage_class=TriageClass.EXPLAINED,
    )

    violation = triage_coverage_violation(
        _decide(_split(), explain), _authority(TriageGrant(262, "needs-human"))
    )

    assert violation is not None and "exactly one" in violation


def test_triaging_an_item_the_run_was_not_granted_is_rejected() -> None:
    violation = triage_coverage_violation(_decide(_split(target=263)), _authority())

    assert violation is not None and "not granted" in violation


def test_a_class_only_lands_on_its_own_action_types() -> None:
    with pytest.raises(ValueError, match="cannot carry triage_class operator_decision"):
        ProposedTechLeadAction(
            id="A1", action_type="escalate_to_human", target_number=262, body="b",
            triage_class=TriageClass.OPERATOR_DECISION,
        ).validate()
    with pytest.raises(ValueError, match="follow_up_issues"):
        ProposedTechLeadAction(
            id="A1", action_type="create_issue", title="t", body="b",
            follow_up_issues=(DecisionFollowUp("t", "b"),),
        ).validate()



def test_a_remedy_is_an_action_on_the_item_never_a_pr_rework() -> None:
    """#7593 review r3: request_rework targets a PR, and the engine refuses a
    blocked issue's rework, so it can be no blocked item's remedy; the
    remedies the triage prompt offers must all be valid."""
    with pytest.raises(ValueError, match="cannot carry triage_class remedy"):
        ProposedTechLeadAction(
            id="A1", action_type="request_rework", target_number=379, target_is_pr=True,
            body="b", finding_ids=("T1",), triage_class=TriageClass.REMEDY,
        ).validate()
    for action_type in ("release_withheld_review", "recover_validated_work", "kill_hung_session", "reset_retry"):
        assert action_type in TRIAGE_CLASS_ACTION_TYPES[TriageClass.REMEDY]
        assert f"`{action_type}`" in render_triage_instructions(TriageAgenda(
            items=(TriageAgendaItem(
                issue_number=262, title="t", labels=("needs-human",), blocking_labels=("needs-human",),
                needs_human_causes=(), fingerprint="needs-human", agent_question=None,
                reason="never triaged", prior=None,
            ),),
            in_force=(), deferred=(),
        ))
    assert "request_rework" not in TRIAGE_CLASS_ACTION_TYPES[TriageClass.REMEDY]


# -- planning: the split question becomes an approvable proposal ----------------


def _plan(decision: TechLeadDecision, *, fingerprints: dict[int, str]):
    config = _config()
    log = CharterDecisionLog(
        run_id="run-1", anchor_issue_number=ANCHOR, decided_at="2026-10-02T12:00:00+00:00",
        triage_fingerprints=fingerprints,
    )
    actions = plan_tech_lead_decision_actions(
        decision, config, LabelManager(config),
        anchor_issue=_issue(ANCHOR, "agent:tech-lead"), expected=build_expected_for_mutation(),
        op_ledger={}, pattern_ledger={}, source_run_id="run-1", source_session_name="tech-lead-900",
        observed_at="2026-10-02T12:00:00+00:00", observed_session_generation=lambda _n: None,
        dedup_corpus=OpenIssueCorpus.ready(()), dedup_grant=DuplicateTargetGrant.of(frozenset()),
        charter_log=log,
    )
    return actions, log


def test_the_split_question_becomes_an_approvable_proposal_with_its_triage_on_record() -> None:
    actions, log = _plan(_decide(_split()), fingerprints={262: "needs-human"})

    [proposal] = [a for a in actions if isinstance(a, CreateTechLeadProposalIssueAction)]
    assert AWAITING_APPROVAL_LABEL in proposal.labels
    assert proposal.op.op_type == "propose_decision"
    assert proposal.op.target_issue_number == 262
    assert proposal.op.decision == OperatorDecision(
        title="Split #262: land the share-page slice under Refs #262",
        body=f"The agent asked: {SPLIT_QUESTION}\n\nRecommend: split.",
        follow_ups=(DecisionFollowUp(
            "Live D1 seller pickup index", "The remainder of #262's acceptance list.",
        ),),
    )
    assert "Decision for #262" in proposal.body and "Live D1 seller pickup index" in proposal.body
    [record] = log.records()
    assert record.outcome is CharterOutcome.PROPOSED
    assert record.lifecycle is CharterProposalLifecycle.AWAITING_APPROVAL
    assert (record.triage_class, record.triage_fingerprint) == (
        TriageClass.OPERATOR_DECISION, "needs-human",
    )


def test_an_approved_decision_plans_its_application() -> None:
    op = _plan(_decide(_split()), fingerprints={262: "needs-human"})[0][0]
    assert isinstance(op, CreateTechLeadProposalIssueAction)

    [action] = plan_approved_tech_lead_op_executions(
        (ApprovedTechLeadOp(proposal_issue_number=950, op=op.op),)
    )

    assert isinstance(action, ApplyOperatorDecisionAction)
    assert (action.issue_number, action.proposal_issue_number) == (262, 950)
    assert action.decision == op.op.decision


# -- applying the operator's approval ------------------------------------------


@dataclass
class _Host:
    issues: dict[int, Issue]
    markers: dict[str, int] = field(default_factory=dict)
    comments: list[tuple[int, str]] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    create_error: Exception | None = None
    comment_failures: set[int] = field(default_factory=set)
    retries: InMemoryTechLeadAuthorityStore = field(default_factory=InMemoryTechLeadAuthorityStore)
    #: Body writes fail (the standing ruling cannot be recorded).
    body_write_error: Exception | None = None

    def get_issue(self, number: int) -> Issue | None:
        return self.issues.get(number)

    def write_body(self, number: int, body: str) -> None:
        self.calls.append(f"body:{number}")
        if self.body_write_error is not None:
            raise self.body_write_error
        self.issues[number].body = body

    def find_issue_by_marker(self, *, title: str, marker: str, authoritative: bool = False) -> int | None:
        assert authoritative, "a miss must prove absence before a create"
        return self.markers.get(marker)

    def create_issue(self, *, title: str, body: str, labels=None, milestone=None) -> dict[str, Any]:
        if self.create_error is not None:
            raise self.create_error
        number = 1000 + len(self.created)
        self.calls.append("create_issue")
        self.created.append({"title": title, "body": body, "labels": labels, "milestone": milestone})
        self.markers[body[body.index("<!--"):]] = number
        return {"number": number}

    def comment_marker_present(self, number: int, marker: str) -> bool:
        return any(n == number and marker in body for n, body in self.comments)

    def apply(self, action):
        from issue_orchestrator.control.actions import ActionResult, AddCommentAction

        assert isinstance(action, AddCommentAction)
        if action.number in self.comment_failures:
            return ActionResult.fail(action, "502")
        self.calls.append(f"comment:{action.number}")
        self.comments.append((action.number, action.comment))
        return ActionResult.ok(action)


def _outcome(status: OperatorCommandStatus, **fields: Any) -> OperatorCommandOutcome:
    return OperatorCommandOutcome(
        intent=OperatorCommandIntent.RETRY, status=status, issue_number=262, **fields,
    )


def _executor(host: _Host, retry, *, held: tuple[str, ...] = (), authority=None) -> OperatorDecisionExecutor:
    config = _config()

    def tracked_retry(number: int) -> OperatorCommandOutcome:
        host.calls.append(f"retry:{number}")
        return retry(number)

    return OperatorDecisionExecutor(
        events=MagicMock(), labels=LabelManager(config), read_issue=host.get_issue,
        retry_issue=tracked_retry, unsettleable_holders=lambda number: held,
        find_issue_by_marker=host.find_issue_by_marker,
        create_issue=host.create_issue, comment_marker_present=host.comment_marker_present,
        apply_action=host.apply, require_authority=authority or (lambda action, number: None),
        retries=host.retries,
        rulings=StandingRulingsOwner(
            read_issue=host.get_issue, write_body=host.write_body, index=InMemoryStandingRulingsIndex(),
        ),
        steps=unused_decision_steps(),
    )


def _approved() -> ApplyOperatorDecisionAction:
    return ApplyOperatorDecisionAction(
        issue_number=262,
        decision=OperatorDecision(
            title="Split #262", body="Land the slice under Refs #262.",
            follow_ups=(DecisionFollowUp("Live D1 seller pickup index", "The remainder."),),
        ),
        proposal_id="A1", anchor_issue_number=950, proposal_issue_number=950,
    )


def _blocked_target() -> Issue:
    target = _issue(262, "agent:backend", "priority:high", "needs-human", "pr-pending")
    target.milestone_number = 4
    return target


def test_approval_publishes_first_retries_last_and_never_retries_twice() -> None:
    """#7593 review F4: the item stays blocked until its decision and follow-ups
    exist, and a replay after the retry never retries again."""
    host = _Host({262: _blocked_target()})
    executor = _executor(host, lambda n: _outcome(OperatorCommandStatus.COMMITTED, removed=("needs-human",)))

    first = executor.apply(_approved())
    again = executor.apply(_approved())  # the finalize failed and the op replays

    assert first.success and again.success and again.details.get("replayed") is True
    assert host.calls == ["create_issue", "comment:262", "body:262", "retry:262", "comment:950"]
    [follow_up] = host.created
    assert follow_up["title"] == "Live D1 seller pickup index"
    assert follow_up["labels"] == ["agent:backend", "priority:high"]  # never workflow state
    assert follow_up["milestone"] == 4
    assert follow_up_marker(950, 1) in follow_up["body"] and "Refs #262" in follow_up["body"]
    decision = next(body for number, body in host.comments if number == 262)
    assert decision_marker(950) in decision and "#1000" in decision
    assert first.details["follow_up_issues"] == ["1000"]


def test_a_replay_after_the_retry_committed_never_retries_again() -> None:
    """#7593 review r2: the retry committed (the item is unblocked) and the
    applied marker failed; a new needs-human was raised before the replay.
    The replay must finish the marker, not retry (which would clear the new
    block), and must not read the unblocked item as a stale approval."""
    host = _Host({262: _blocked_target()}, comment_failures={950})
    executor = _executor(host, lambda n: _outcome(OperatorCommandStatus.COMMITTED, removed=("needs-human",)))

    assert not executor.apply(_approved()).success
    host.issues[262] = _issue(262, "agent:backend", "needs-human")  # raised since
    host.comment_failures.clear()
    replay = executor.apply(_approved())

    assert replay.success and replay.details["replayed"] is True
    assert host.calls == ["create_issue", "comment:262", "body:262", "retry:262", "comment:950"]


class _EngineStopped(BaseException):
    """The process dying between the retry's GitHub write and its receipt."""


def _retry_then_stop(host: _Host):
    def retry(number: int) -> OperatorCommandOutcome:
        host.issues[number] = _issue(number, "agent:backend", "priority:high")  # labels came off
        raise _EngineStopped

    return retry


def test_a_replay_after_the_engine_stopped_mid_retry_never_retries_a_new_block() -> None:
    """#7593 review r3: the retry committed and the engine stopped before it
    was recorded; the item asked again since. The replay cannot tell that from
    a retry that never happened, so it retries nothing, keeps the new
    needs-human, and keeps the proposal (a failure, never a stale close)."""
    host = _Host({262: _blocked_target()})
    with pytest.raises(_EngineStopped):
        _executor(host, _retry_then_stop(host)).apply(_approved())
    host.issues[262] = _issue(262, "agent:backend", "needs-human")  # raised since

    replay = _executor(host, lambda n: pytest.fail("retried twice")).apply(_approved())

    assert not replay.success and replay.details.get("mode") != STALE_DOWNGRADE_MODE
    assert "not retried a second time" in (replay.error or "")
    assert host.issues[262].labels == ["agent:backend", "needs-human"]
    assert host.calls.count("retry:262") == 1


def test_a_replay_after_the_engine_stopped_mid_retry_finishes_an_unblocked_item() -> None:
    """The same interruption, but the item is still unblocked: that is the
    retry's own work, so the replay finishes the applied marker instead of
    closing the approval as stale."""
    host = _Host({262: _blocked_target()})
    with pytest.raises(_EngineStopped):
        _executor(host, _retry_then_stop(host)).apply(_approved())

    replay = _executor(host, lambda n: pytest.fail("retried twice")).apply(_approved())

    assert replay.success and replay.details["replayed"] is True
    assert host.calls == ["create_issue", "comment:262", "body:262", "retry:262", "comment:950"]
    assert host.retries.decision_retry_state(proposal_issue_number=950) is DecisionRetryState.COMMITTED


def test_an_unsettled_retry_may_be_attempted_again() -> None:
    """A retry that settled without committing is abandoned, not left begun,
    so its replay retries normally rather than handing the proposal back."""
    host = _Host({262: _blocked_target()})
    incomplete = _outcome(OperatorCommandStatus.INCOMPLETE, failed=("pr-pending",))
    assert not _executor(host, lambda n: incomplete).apply(_approved()).success

    replay = _executor(host, lambda n: _outcome(OperatorCommandStatus.COMMITTED)).apply(_approved())

    assert replay.success and host.calls.count("retry:262") == 2


def test_a_failed_follow_up_leaves_the_item_blocked_and_the_replay_resumes() -> None:
    host = _Host({262: _blocked_target()}, create_error=RuntimeError("502"))
    executor = _executor(host, lambda n: _outcome(OperatorCommandStatus.COMMITTED))

    assert not executor.apply(_approved()).success
    assert host.calls == []  # nothing posted, nothing retried

    host.create_error = None
    assert executor.apply(_approved()).success
    assert host.calls == ["create_issue", "comment:262", "body:262", "retry:262", "comment:950"]


def test_a_lost_claim_files_no_follow_up() -> None:
    """#7593 review F5: each follow-up is behind the applier's mutation gate."""
    from issue_orchestrator.control.claim_gate import ClaimLostError

    def refuse(action, number):
        raise ClaimLostError(number, "another engine holds it")

    host = _Host({262: _blocked_target()})
    executor = _executor(host, lambda n: _outcome(OperatorCommandStatus.COMMITTED), authority=refuse)

    with pytest.raises(ClaimLostError):
        executor.apply(_approved())
    assert host.created == [] and host.calls == []


@pytest.mark.parametrize(
    ("labels", "held", "why"),
    [
        (("agent:backend", "needs-human"), ("claim_quarantine",), "claim_quarantine"),
        (("agent:backend",), (), "no longer blocked"),
    ],
)
def test_an_approval_that_cannot_be_carried_out_closes_stale_with_no_writes(labels, held, why) -> None:
    host = _Host({262: _issue(262, *labels)})
    executor = _executor(host, lambda n: _outcome(OperatorCommandStatus.COMMITTED), held=held)

    result = executor.apply(_approved())

    assert not result.success and result.details["mode"] == "stale_downgrade"
    assert why in result.details["skip_reason"]
    assert host.calls == []


def test_a_retry_github_would_not_settle_is_a_retried_failure() -> None:
    host = _Host({262: _blocked_target()})
    executor = _executor(host, lambda n: _outcome(
        OperatorCommandStatus.INCOMPLETE, failed=("blocked-failed",),
    ))

    result = executor.apply(_approved())

    assert not result.success and "mode" not in result.details
    assert "comment:950" not in host.calls  # not marked applied


# -- the whole completion contract: scope + coverage, as validation applies it --


def test_a_health_review_may_act_on_its_granted_items_and_nothing_else() -> None:
    from issue_orchestrator.control.tech_lead_completion import validate_decision_for_authority

    config = _config()
    authority = _authority(TriageGrant(179, ""), TriageGrant(262, "needs-human"))
    labels = LabelManager(config)

    assert validate_decision_for_authority(
        _decide(_split(), _hand_over("A2", 179)), authority, config=config, labels=labels,
    ) is None
    outside = ProposedTechLeadAction(
        id="A3", action_type="post_comment", target_number=500, body="An unrelated issue.",
    )
    violation = validate_decision_for_authority(
        _decide(_split(), _hand_over("A2", 179), outside), authority, config=config, labels=labels,
    )
    assert violation is not None and "#500" in violation
    assert validate_decision_for_authority(
        _decide(_split()), _authority(), config=config, labels=labels,
    ) is not None  # no grant, no act-level authority over #262



def test_an_owed_triage_keeps_a_review_due_on_an_unchanged_board() -> None:
    """#7593 review F1: items deferred by the per-run cap (or whose triage did
    not take effect) leave the board fingerprint unchanged; the review must
    still come back for them."""
    from issue_orchestrator.control.blocked_item_triage import triage_owed
    from issue_orchestrator.control.health_review_trigger import health_review_decision

    config = _config()
    config.tech_lead.health_review.interval_minutes = 60
    state = OrchestratorState()
    state.cached_scope_issues = [_issue(n, "agent:backend", "blocked-failed") for n in range(100, 111)]
    reviewed = health_review_decision(config, state, 1000.0).fingerprint
    state.last_health_review_at = 1000.0
    state.last_reviewed_board_fingerprint = reviewed

    ledger = _Ledger({
        n: [_triage_record(n, TriageClass.EXPLAINED, f"blocked-failed@{_onset('blocked-failed')}", effect="applied")]
        for n in range(100, 100 + MAX_TRIAGE_ITEMS_PER_RUN)
    })
    assert triage_owed(config, state, _Authority(ledger), _episode_owner(_Episodes())) is True  # the three deferred ones
    assert health_review_decision(
        config, state, 1000.0 + 3600, triage_owed=lambda: triage_owed(config, state, _Authority(ledger), _episode_owner(_Episodes())),
    ).due is True

    everything = _Ledger({
        n: [_triage_record(n, TriageClass.EXPLAINED, f"blocked-failed@{_onset('blocked-failed')}", effect="applied")]
        for n in range(100, 111)
    })
    assert triage_owed(config, state, _Authority(everything), _episode_owner(_Episodes())) is False
    assert health_review_decision(
        config, state, 1000.0 + 3600, triage_owed=lambda: triage_owed(config, state, _Authority(everything), _episode_owner(_Episodes())),
    ).due is False


def test_a_different_decision_for_the_same_item_is_its_own_proposal() -> None:
    """#7593 review F2: approval runs the STORED decision, so a re-proposal of a
    different one must not be recorded as waiting on the old proposal."""
    from issue_orchestrator.control.required_issue_comment import ReuseTechLeadProposalAction
    from issue_orchestrator.control.tech_lead_proposals import build_op_ledger

    first, _ = _plan(_decide(_split()), fingerprints={262: "needs-human"})
    [filed] = [a for a in first if isinstance(a, CreateTechLeadProposalIssueAction)]
    ledger = build_op_ledger(((950, filed.op),))

    def replan(decision: TechLeadDecision):
        config = _config()
        return plan_tech_lead_decision_actions(
            decision, config, LabelManager(config),
            anchor_issue=_issue(ANCHOR, "agent:tech-lead"), expected=build_expected_for_mutation(),
            op_ledger=ledger, pattern_ledger={}, source_run_id="run-2", source_session_name="tl-2",
            observed_at="2026-10-02T13:00:00+00:00", observed_session_generation=lambda _n: None,
            dedup_corpus=OpenIssueCorpus.ready(()), dedup_grant=DuplicateTargetGrant.of(frozenset()),
        )

    same = replan(_decide(_split()))
    assert any(isinstance(a, ReuseTechLeadProposalAction) and a.number == 950 for a in same)

    different = ProposedTechLeadAction(
        id="A1", action_type="propose_decision", target_number=262,
        title="Do not split #262: finish the live index first", body="Recommend: keep it whole.",
        triage_class=TriageClass.OPERATOR_DECISION,
    )
    planned = replan(_decide(different))
    assert not any(isinstance(a, ReuseTechLeadProposalAction) for a in planned)
    [new] = [a for a in planned if isinstance(a, CreateTechLeadProposalIssueAction)]
    assert new.op.decision is not None and new.op.decision.title.startswith("Do not split")


def test_an_approved_decision_becomes_the_items_standing_ruling() -> None:
    """#8141: porchpin#364's approved decision (#456) was posted as a comment and
    never bound the later rework or review. It is in the item's body now."""
    from issue_orchestrator.domain.standing_ruling import RulingAuthority, parse_rulings_block

    host = _Host({262: _blocked_target()})
    executor = _executor(host, lambda n: _outcome(OperatorCommandStatus.COMMITTED, removed=("needs-human",)))

    assert executor.apply(_approved()).success

    [ruling] = parse_rulings_block(host.issues[262].body)
    assert ruling.ruling_id == "pd-950" and ruling.authority is RulingAuthority.APPROVED_DECISION
    assert "Split #262" in ruling.text and "Land the slice under Refs #262." in ruling.text
    assert "proposal #950" in ruling.source


def test_a_ruling_github_would_not_keep_retries_nothing() -> None:
    host = _Host({262: _blocked_target()}, body_write_error=RuntimeError("GitHub kept the old body"))
    executor = _executor(host, lambda n: _outcome(OperatorCommandStatus.COMMITTED, removed=("needs-human",)))

    result = executor.apply(_approved())

    assert not result.success and "standing ruling not recorded" in (result.error or "")
    assert "retry:262" not in host.calls


def test_the_triage_agenda_names_each_items_standing_rulings() -> None:
    from issue_orchestrator.domain.blocked_item_triage import render_triage_instructions
    from tests.standing_ruling_helpers import a_ruling

    ruling = a_ruling(text="Runtime stamping replaces the walk checker.\n\nNever extend the walk checker.")
    owner = _owner([_issue(262, "agent:backend", "needs-human")], rulings={262: (ruling,)})

    agenda = owner.agenda(anchor_issue_number=950)

    [item] = agenda.items
    assert item.to_dict()["standing_rulings"] == list(item.standing_rulings)
    assert "Never extend the walk checker." in item.standing_rulings[0]  # the full text, not a summary line
    rendered = render_triage_instructions(agenda)
    # #8347: the prompt points at the binding section of the covered work's
    # rulings (read for each launch, a retry's too); it never copies them.
    assert "it has standing rulings: see the binding section on issue #262" in rendered
    assert "Never extend the walk checker." not in rendered
