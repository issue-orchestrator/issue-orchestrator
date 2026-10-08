"""#8688 scenario: a triaged item is unblocked, re-blocked with the SAME cause,
and is owed a new triage.

Real owners end to end: the shared needs-human block (:class:`NeedsHumanBlock`)
over the real SQLite cause store, the triage owner's agenda and the health
review's ``triage_owed`` rule, and the in-memory charter ledger the triage is
recorded in. Only GitHub's labels are a dict.
"""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.control.blocked_item_triage import (
    StateBlockedItemTriage,
    open_proposal_index,
    triage_owed,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.needs_human_block import NeedsHumanBlock
from issue_orchestrator.domain.human_block import (
    BlockOutcome,
    HumanBlockRequest,
    NeedsHumanCause,
)
from issue_orchestrator.domain.models import Issue, OrchestratorState
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


class _Labels:
    def __init__(self) -> None:
        self.live: dict[int, set[str]] = {}

    def add_label(self, issue_number: int, label: str) -> None:
        self.live.setdefault(issue_number, set()).add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.live.setdefault(issue_number, set()).discard(label)


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
    github = _Labels()
    github.live[ITEM] = {"agent:backend"}
    store = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    block = NeedsHumanBlock(
        needs_human_label=labels.needs_human,
        tech_lead_marker=labels.tech_lead_needs_human,
        labels=github,
        read_labels=lambda number: sorted(github.live.get(number, set())),
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
        episodes=store,
    )
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
    assert triage_owed(config, state, authority, store) is False

    # 2. The block is lifted (the agent's cause is released: the label comes off).
    assert block.release(question) is BlockOutcome.CLEARED
    observe()
    assert store.needs_human_episodes([ITEM]) == {}

    # 3. A new agent question re-blocks it with the SAME label and cause.
    assert block.acquire(question) is BlockOutcome.HELD
    observe()
    assert labels.needs_human in github.live[ITEM]

    assert triage_owed(config, state, authority, store) is True
    agenda = triage.agenda(anchor_issue_number=ANCHOR)
    [item] = agenda.items
    assert item.issue_number == ITEM and agenda.in_force == ()
    assert item.fingerprint != granted.fingerprint
    assert item.reason.startswith("it was blocked again, under the same labels")

    # 4. The triage of the new episode covers it again.
    authority.charter_ledger.record_decisions([
        _explained(item.fingerprint, "run-2", "2026-10-08T01:11:17+00:00"),
    ])
    assert triage_owed(config, state, authority, store) is False


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
    assert store.adopt_needs_human_episodes([ITEM, 451]) == {
        ITEM: second, 451: store.needs_human_episodes([451])[451],
    }  # adoption never replaces a recorded generation
