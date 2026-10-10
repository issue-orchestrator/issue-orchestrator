"""#8688/#8731 scenarios: a triaged item is unblocked, re-blocked with the SAME
cause, and is owed a new triage, whether its block is the shared needs-human
block or another blocking label.

Real owners end to end: the shared needs-human block (:class:`NeedsHumanBlock`)
over the real SQLite cause store, the triage owner's agenda and the health
review's ``triage_owed`` rule, and the in-memory charter ledger the triage is
recorded in. Only GitHub is faked: its labels, and the ``labeled`` event that
put each standing label on.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from issue_orchestrator.control.block_episodes import BlockEpisodes
from issue_orchestrator.control.blocked_item_triage import (
    StateBlockedItemTriage,
    open_proposal_index,
    triage_owed,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.needs_human_block import NeedsHumanBlock
from issue_orchestrator.control.needs_human_episodes import NeedsHumanEpisodes
from issue_orchestrator.domain.human_block import (
    BlockOutcome,
    HumanBlockRequest,
    NeedsHumanCause,
)
from issue_orchestrator.domain.models import Issue, OrchestratorState
from issue_orchestrator.domain.blocked_item_triage import BlockEpisode
from issue_orchestrator.domain.tech_lead_approval import LabelEvent
from issue_orchestrator.domain.tech_lead_artifacts import TriageClass
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
    TechLeadCharterDecision,
)
from issue_orchestrator.execution.pending_work_claim_store import SqlitePendingWorkClaimStore
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore

ITEM = 450
ANCHOR = 900


class _GitHub:
    """Labels, and the event that applied each one standing now."""

    def __init__(self) -> None:
        self.live: dict[int, set[str]] = {}
        self.applied: dict[tuple[int, str], LabelEvent] = {}
        self._events = 0

    def add_label(self, issue_number: int, label: str) -> None:
        if label not in self.live.setdefault(issue_number, set()):
            self._events += 1
            self.applied[(issue_number, label)] = LabelEvent(
                event_id=self._events, actor_login="engine[bot]", actor_is_bot=True,
                created_at=f"2026-10-0{self._events}T00:00:00Z", actor_id=9,
            )
        self.live[issue_number].add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.live.setdefault(issue_number, set()).discard(label)
        self.applied.pop((issue_number, label), None)

    def label_application(self, issue_number: int, label: str) -> LabelEvent | None:
        return self.applied.get((issue_number, label))

    def label_applications(self, issue_number: int, labels: Sequence[str]) -> dict[str, LabelEvent | None]:
        return {label: self.label_application(issue_number, label) for label in labels}


def _episodes(
    store: SqlitePendingWorkClaimStore,
    github: _GitHub,
    labels: LabelManager,
    *,
    recheck_seconds: float,
    needs_human_reads: Callable[[int, Sequence[str]], dict[str, LabelEvent | None]] | None = None,
) -> BlockEpisodes:
    """The block-episode owner as the composition builds it, over *github*."""
    return BlockEpisodes(
        needs_human=NeedsHumanEpisodes(
            store=store, label_applications=needs_human_reads or github.label_applications,
            labels=labels,
        ),
        label_applications=github.label_applications, labels=labels,
        clock=lambda: 0.0, recheck_seconds=lambda: recheck_seconds,
    )


def _explained(fingerprint: str, run: str, decided_at: str) -> TechLeadCharterDecision:
    """The charter record an applied ``explained`` triage of #450 leaves."""
    return TechLeadCharterDecision(
        decision_id=f"decision:{run}:A1", source=CharterDecisionSource.DECISION,
        run_id=run, action_id="A1", anchor_issue_number=ANCHOR, target_number=ITEM,
        target_is_pr=False, action_kind="post_comment", role=CharterRole.FLOW,
        required_depth=CharterDepth.WORKAROUND, binding=CharterBinding.FLOOR,
        role_enabled=True, role_depth=CharterDepth.RESTRUCTURE,
        role_authority=CharterAuthority.EXECUTE, action_ceiling=CharterAuthority.EXECUTE,
        ceiling_source="c", outcome=CharterOutcome.EXECUTED,
        reason_code=CharterReason.FLOOR_ALWAYS_EXECUTES, reason="r", decided_at=decided_at,
        execution=CharterExecutionResult("applied"),
        triage_class=TriageClass.EXPLAINED, triage_fingerprint=fingerprint,
    )


def test_a_triaged_item_unblocked_then_reblocked_with_the_same_cause_is_owed_a_new_triage(
    tmp_path: Path,
) -> None:
    config = Config()
    config.repo = "porchpin/porchpin"
    config.tech_lead_review_agent = "agent:tech-lead"
    labels = LabelManager(config)
    github = _GitHub()
    github.live[ITEM] = {"agent:backend"}
    store = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    block = NeedsHumanBlock(
        needs_human_label=labels.needs_human,
        tech_lead_marker=labels.tech_lead_needs_human,
        labels=github,
        read_labels=lambda number: sorted(github.live.get(number, set())),
        label_application=github.label_application,
        quarantined_issue_numbers=frozenset,
        causes=store,
    )
    authority = InMemoryTechLeadAuthorityStore()
    state = OrchestratorState()

    def observe() -> None:  # the tick's issue refresh
        state.cached_scope_issues = [Issue(
            number=ITEM, title="Re-run PR #521's failed Windows job",
            labels=sorted(github.live[ITEM]), repo=config.repo, state="open",
        )]

    triage = StateBlockedItemTriage(
        config=config, state=lambda: state, labels=labels,
        needs_human_causes=block.recorded_causes, charter_ledger=authority.charter_ledger,
        open_proposals=lambda: open_proposal_index(authority),
        timeline_reader=lambda number, limit: [], standing_rulings=lambda number: (),
        episodes=_episodes(store, github, labels, recheck_seconds=3600),
    )
    # The tick's check verifies against GitHub on every call here (a zero
    # recheck period), as it does once per interval in production.
    episodes = _episodes(store, github, labels, recheck_seconds=0)
    question = HumanBlockRequest(
        target=ITEM, cause=NeedsHumanCause.AGENT_COMPLETION, reason="Agent requested human input",
    )

    # 1. The agent asks; the block is granted to a health review and triaged.
    assert block.acquire(question) is BlockOutcome.HELD
    observe()
    [granted] = triage.agenda(anchor_issue_number=ANCHOR).grants
    authority.charter_ledger.record_decisions([
        _explained(granted.fingerprint, "run-1", "2026-10-04T14:50:00+00:00"),
    ])
    assert triage.agenda(anchor_issue_number=ANCHOR).in_force == (ITEM,)
    assert triage_owed(config, state, authority, episodes) is False

    # 2. The block is lifted (the agent's cause is released: the label comes off).
    assert block.release(question) is BlockOutcome.CLEARED
    observe()
    assert store.needs_human_episodes([ITEM]) == {}

    # 3. A new agent question re-blocks it with the SAME label and cause.
    assert block.acquire(question) is BlockOutcome.HELD
    observe()
    assert labels.needs_human in github.live[ITEM]

    assert triage_owed(config, state, authority, episodes) is True
    agenda = triage.agenda(anchor_issue_number=ANCHOR)
    [item] = agenda.items
    assert item.issue_number == ITEM and agenda.in_force == ()
    assert item.fingerprint != granted.fingerprint
    assert item.reason.startswith("it was blocked again, under the same labels")

    # 4. The triage of the new episode covers it again.
    authority.charter_ledger.record_decisions([
        _explained(item.fingerprint, "run-2", "2026-10-08T01:11:17+00:00"),
    ])
    assert triage_owed(config, state, authority, episodes) is False
    assert triage.agenda(anchor_issue_number=ANCHOR).in_force == (ITEM,)

    # 5. An operator takes the label off and puts it back BY HAND, between the
    # owner's observations: the owner never sees it, GitHub's events do.
    github.remove_label(ITEM, labels.needs_human)
    github.add_label(ITEM, labels.needs_human)
    agenda = triage.agenda(anchor_issue_number=ANCHOR)
    [again] = agenda.items
    assert again.issue_number == ITEM and agenda.in_force == ()
    assert again.fingerprint not in {granted.fingerprint, item.fingerprint}
    authority.charter_ledger.record_decisions([
        _explained(again.fingerprint, "run-3", "2026-10-08T02:00:00+00:00"),
    ])
    assert triage_owed(config, state, authority, episodes) is False

    # 6. Again by hand: the tick's own recheck sees it, with no review run.
    github.remove_label(ITEM, labels.needs_human)
    github.add_label(ITEM, labels.needs_human)
    assert triage_owed(config, state, authority, episodes) is True


def test_the_owner_cannot_reopen_a_generation_while_it_is_being_bound(tmp_path: Path) -> None:
    """#8688 review r2 F2: the binding holds the block owner's per-issue gate
    across the GitHub read, so the owner cannot end the generation and open
    a new one between the read and the bind (and bind it to an old event)."""
    config = Config()
    labels = LabelManager(config)
    github = _GitHub()
    store = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    block = NeedsHumanBlock(
        needs_human_label=labels.needs_human, tech_lead_marker=labels.tech_lead_needs_human,
        labels=github, read_labels=lambda number: sorted(github.live.get(number, set())),
        label_application=github.label_application,
        quarantined_issue_numbers=frozenset, causes=store,
    )
    question = HumanBlockRequest(
        target=ITEM, cause=NeedsHumanCause.AGENT_COMPLETION, reason="Agent requested human input",
    )
    assert block.acquire(question) is BlockOutcome.HELD
    [opened] = store.needs_human_episodes([ITEM]).values()
    during: list[BlockOutcome] = []

    def read_then_race(number: int, labels: Sequence[str]) -> dict[str, LabelEvent | None]:
        standing = github.label_applications(number, labels)
        during.append(block.release(question))  # the owner tries to lift it now
        return standing

    episodes = _episodes(store, github, labels, recheck_seconds=0, needs_human_reads=read_then_race)
    issue = Issue(number=ITEM, title="t", labels=[labels.needs_human], repo="r/r", state="open")
    assert episodes.verified({ITEM: issue}) == {ITEM: BlockEpisode(opened)}
    assert during == [BlockOutcome.FAILED]  # the owner was held off
    assert labels.needs_human in github.live[ITEM]


def test_a_self_recording_cause_still_dates_its_generation(tmp_path: Path) -> None:
    """A tech-lead hand-over records no cause row, but it opens a new episode
    when it puts the label back on, and that episode ends with the label."""
    store = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    store.restart_needs_human_causes(ITEM, "agent_completion", reason="asked")
    [first] = store.needs_human_episodes([ITEM]).values()

    store.clear_needs_human_causes(ITEM)
    assert store.needs_human_episodes([ITEM]) == {}
    assert ITEM not in store.needs_human_cause_targets()
    store.open_needs_human_generation(ITEM)

    [second] = store.needs_human_episodes([ITEM]).values()
    assert second != first and store.needs_human_causes(ITEM) == frozenset()
    assert store.needs_human_cause_targets() == frozenset({ITEM})  # the reconcile retires it
    # Binding: the owner's own write binds an unbound generation, keeping its
    # episode; the same event keeps it again; a different event (a
    # re-application) replaces it; a label with no generation gets one dated
    # by its event.
    store.bind_needs_human_generation(
        ITEM, event_id=5, applied_at="2026-10-05T00:00:00Z", own_write=True,
    )
    assert store.needs_human_episodes([ITEM])[ITEM] == second
    assert store.bind_needs_human_episode(ITEM, event_id=5, applied_at="2026-10-05T00:00:00Z") == second
    third = store.bind_needs_human_episode(ITEM, event_id=6, applied_at="2026-10-06T00:00:00Z")
    assert third != second and third.startswith("2026-10-06T00:00:00Z#")
    assert store.bind_needs_human_episode(451, event_id=9, applied_at="2026-10-07T00:00:00Z").startswith(
        "2026-10-07T00:00:00Z#"
    )
    # An unbound generation whose own write was never bound is ended by any
    # other binding: a person's re-application would look the same (#8774).
    store.open_needs_human_generation(452)
    [unbound] = store.needs_human_episodes([452]).values()
    assert store.bind_needs_human_episode(452, event_id=10, applied_at="2026-10-08T00:00:00Z") != unbound


def test_a_publish_failed_block_that_recurs_after_a_successful_retry_is_owed_a_new_triage(
    tmp_path: Path,
) -> None:
    """#8731 scenario: ``publish-failed`` with no needs-human beside it. The
    block is triaged; a retry publishes and lifts the label; the next publish
    fails and the label comes back alone. Same labels, a new episode: the
    item is owed a triage, by the agenda and by the tick's own recheck."""
    config = Config()
    config.repo = "porchpin/porchpin"
    config.tech_lead_review_agent = "agent:tech-lead"
    labels = LabelManager(config)
    github = _GitHub()
    github.live[ITEM] = {"agent:backend"}
    store = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    authority = InMemoryTechLeadAuthorityStore()
    state = OrchestratorState()

    def observe() -> None:
        state.cached_scope_issues = [Issue(
            number=ITEM, title="Publish the share page", labels=sorted(github.live[ITEM]),
            repo=config.repo, state="open",
        )]

    triage = StateBlockedItemTriage(
        config=config, state=lambda: state, labels=labels,
        needs_human_causes=lambda numbers: {}, charter_ledger=authority.charter_ledger,
        open_proposals=lambda: open_proposal_index(authority),
        timeline_reader=lambda number, limit: [], standing_rulings=lambda number: (),
        episodes=_episodes(store, github, labels, recheck_seconds=3600),
    )
    episodes = _episodes(store, github, labels, recheck_seconds=0)

    # 1. The publish fails; the block is granted and triaged.
    github.add_label(ITEM, "publish-failed")
    observe()
    [granted] = triage.agenda(anchor_issue_number=ANCHOR).grants
    authority.charter_ledger.record_decisions([
        _explained(granted.fingerprint, "run-1", "2026-10-04T14:50:00+00:00"),
    ])
    assert triage.agenda(anchor_issue_number=ANCHOR).in_force == (ITEM,)
    assert triage_owed(config, state, authority, episodes) is False

    # 2. A retry publishes and lifts the block; 3. the next publish fails again.
    github.remove_label(ITEM, "publish-failed")
    observe()
    assert triage.agenda(anchor_issue_number=ANCHOR).items == ()
    github.add_label(ITEM, "publish-failed")
    observe()

    assert triage_owed(config, state, authority, episodes) is True
    agenda = triage.agenda(anchor_issue_number=ANCHOR)
    [item] = agenda.items
    assert item.issue_number == ITEM and agenda.in_force == ()
    assert item.fingerprint != granted.fingerprint
    assert item.reason.startswith("it was blocked again, under the same labels")

    # 4. The triage of the new episode covers it again, with no store row.
    authority.charter_ledger.record_decisions([
        _explained(item.fingerprint, "run-2", "2026-10-08T01:11:17+00:00"),
    ])
    assert triage_owed(config, state, authority, episodes) is False
    assert store.needs_human_episodes([ITEM]) == {}
