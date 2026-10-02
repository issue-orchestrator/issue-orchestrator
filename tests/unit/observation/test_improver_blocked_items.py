"""Assembling ``blocked-items.json`` from records already read (#7490)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from issue_orchestrator.observation.engine_audit import BlockedLane
from issue_orchestrator.observation.improver_blocked_items import blocked_items_input
from issue_orchestrator.ports.engine_audit import OpenIssueLabels, TimelineEvent
from issue_orchestrator.ports.pending_work_claim_store import NeedsHumanCauseRow
from issue_orchestrator.ports.timeline_store import TimelineRecord

CUTOFF = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
LANE = BlockedLane.of(None)
ISSUES = [
    OpenIssueLabels(number=1, title="not blocked", labels=("bug", "in-progress")),
    OpenIssueLabels(number=262, title="asks", labels=("needs-human",)),
    OpenIssueLabels(number=400, title="failed", labels=("blocked:claim-lost",)),
]


def _event(issue: int, name: str, at: datetime, **data: object) -> TimelineEvent:
    return TimelineEvent(
        issue_number=issue,
        record=TimelineRecord(event_id=f"{name}-{at}", timestamp=at.isoformat(), event=name, data=dict(data),
                              source_event=name),
    )


def test_only_blocked_issues_are_items_and_every_blocking_label_counts() -> None:
    staged = blocked_items_input(
        ISSUES, lane=LANE, causes=(), ledger=(), case_files=None, timeline=(), cutoff=CUTOFF, coverage_proven=False
    )

    assert [i.number for i in staged.items] == [262, 400]
    assert [b.label for b in staged.items[1].blocking_labels] == ["blocked:claim-lost"]


def test_an_unread_source_is_named_and_its_facts_left_unknown() -> None:
    staged = blocked_items_input(
        ISSUES, lane=LANE, causes="claim store unreadable: locked", ledger="tech-lead store absent",
        case_files=None, timeline="absent: no timeline.sqlite", cutoff=CUTOFF, coverage_proven=False,
    )

    item = staged.items[0]
    assert item.needs_human_causes is None and "locked" in staged.causes_coverage.detail
    assert item.blocked_since is None and item.blocking_labels[0].since_at is None
    assert "no timeline.sqlite" in item.timeline_coverage.detail and not item.timeline_coverage.complete
    assert item.decisions == () and "tech-lead store absent" in staged.decisions_coverage.detail


def test_only_the_items_own_cause_rows_are_staged() -> None:
    rows = (NeedsHumanCauseRow(262, "agent_completion", "asked"), NeedsHumanCauseRow(9, "session_lifecycle", "x"))

    staged = blocked_items_input(
        ISSUES, lane=LANE, causes=rows, ledger=(), case_files=None, timeline=(), cutoff=CUTOFF, coverage_proven=False
    )

    assert [(c.cause, c.reason) for c in staged.items[0].needs_human_causes or ()] == [("agent_completion", "asked")]


def test_the_onset_is_the_last_put_on_that_was_never_taken_off() -> None:
    t0 = CUTOFF - timedelta(days=3)
    events = [
        _event(262, "issue.labels_changed", t0, added=["needs-human"], removed=[]),
        _event(262, "issue.labels_changed", t0 + timedelta(hours=1), added=[], removed=["needs-human"]),
        _event(262, "issue.needs_human", t0 + timedelta(hours=5), question="Split it?"),
        _event(262, "issue.labels_changed", t0 + timedelta(hours=5, seconds=3), added=["needs-human"], removed=[]),
        _event(262, "issue.labels_changed", t0 + timedelta(hours=7), added=["in-progress"], removed=[]),
    ]

    staged = blocked_items_input(
        ISSUES, lane=LANE, causes=(), ledger=(), case_files=None, timeline=events, cutoff=CUTOFF, coverage_proven=False
    )

    item = staged.items[0]
    assert item.blocked_since == t0 + timedelta(hours=5, seconds=3)
    assert item.blocking_labels[0].since_event == "issue.labels_changed"
    # Label changes that touch no blocking label are not block events.
    assert [e.event for e in item.block_events] == [
        "issue.labels_changed", "issue.labels_changed", "issue.needs_human", "issue.labels_changed",
    ]
    assert "question: Split it?" in item.block_events[2].detail
    assert item.timeline_coverage.from_ == t0


def test_a_needs_human_request_alone_is_no_onset() -> None:
    """r6 F1: the escalation reconciler emits the request for a label already
    on (put on before the retained timeline began): it dates nothing."""
    events = [_event(262, "issue.needs_human", CUTOFF - timedelta(hours=2), question="q")]

    staged = blocked_items_input(
        ISSUES, lane=LANE, causes=(), ledger=(), case_files=None, timeline=events, cutoff=CUTOFF, coverage_proven=False
    )

    assert staged.items[0].blocking_labels[0].since_at is None and staged.items[0].blocked_since is None


def test_a_recorded_add_is_a_fresh_onset_even_with_no_recorded_removal() -> None:
    """porchpin #364: needs-human added 09-23, taken off by hand on GitHub (no
    timeline record), added again by the engine 10-02. The engine adds only
    an absent label, so the block began on 10-02, not 09-23."""
    t0 = CUTOFF - timedelta(days=9)
    readded = CUTOFF - timedelta(hours=4)
    events = [
        _event(262, "issue.labels_changed", t0, added=["needs-human"], removed=[]),
        _event(262, "issue.labels_changed", readded, added=["needs-human"], removed=[]),
        # A later request while it is on does not move it.
        _event(262, "issue.needs_human", readded + timedelta(minutes=1), question="q"),
    ]

    staged = blocked_items_input(
        ISSUES, lane=LANE, causes=(), ledger=(), case_files=None, timeline=events, cutoff=CUTOFF, coverage_proven=False
    )

    assert staged.items[0].blocked_since == readded


def test_the_engines_blocked_lane_decides_and_the_tech_leads_own_artefacts_are_not_items() -> None:
    issues = [
        OpenIssueLabels(number=10, title="held", labels=("recovery-pending",)),
        OpenIssueLabels(number=11, title="legacy", labels=("publish-failed",)),
        OpenIssueLabels(number=12, title="marker", labels=("tech-lead-needs-human",)),
        OpenIssueLabels(number=13, title="proposal", labels=("proposed-tech-lead",)),
        OpenIssueLabels(number=14, title="case file", labels=("tech-lead-observation",)),
    ]

    staged = blocked_items_input(
        issues, lane=LANE, causes=(), ledger=(), case_files=None, timeline=(), cutoff=CUTOFF, coverage_proven=False
    )

    assert [i.number for i in staged.items] == [10, 11, 12]


def test_an_add_that_could_not_read_presence_moves_no_known_onset() -> None:
    """r1 F4: the applier records an add even when its presence read failed;
    the label may already have been on, so the earlier onset stands."""
    t0 = CUTOFF - timedelta(days=2)
    events = [
        _event(262, "issue.labels_changed", t0, added=["needs-human"], removed=[]),
        _event(262, "issue.labels_changed", t0 + timedelta(days=1), added=["needs-human"], removed=[],
               presence_unknown=True),
    ]

    staged = blocked_items_input(
        ISSUES, lane=LANE, causes=(), ledger=(), case_files=None, timeline=events, cutoff=CUTOFF, coverage_proven=False
    )

    assert staged.items[0].blocked_since == t0


def test_an_add_that_could_not_read_presence_is_no_onset() -> None:
    """r2 F2: with no earlier onset retained, an uncertain add leaves it unknown."""
    events = [
        _event(262, "issue.labels_changed", CUTOFF - timedelta(hours=3), added=["needs-human"], removed=[],
               presence_unknown=True),
    ]

    staged = blocked_items_input(
        ISSUES, lane=LANE, causes=(), ledger=(), case_files=None, timeline=events, cutoff=CUTOFF, coverage_proven=False
    )

    assert staged.items[0].blocking_labels[0].since_at is None and staged.items[0].blocked_since is None


def test_a_needs_human_request_after_an_uncertain_add_is_no_onset() -> None:
    """r4 F2: the request follows an add that may have found the label on."""
    t = CUTOFF - timedelta(hours=3)
    events = [
        _event(262, "issue.labels_changed", t, added=["needs-human"], removed=[], presence_unknown=True),
        _event(262, "issue.needs_human", t + timedelta(seconds=2), question="q"),
    ]

    staged = blocked_items_input(
        ISSUES, lane=LANE, causes=(), ledger=(), case_files=None, timeline=events, cutoff=CUTOFF, coverage_proven=False
    )

    assert staged.items[0].blocked_since is None


def test_a_label_github_returns_in_another_case_keeps_its_onset() -> None:
    """r4 F3: GitHub folds label case; the engine recorded needs-human."""
    t = CUTOFF - timedelta(hours=3)
    issues = [OpenIssueLabels(number=262, title="asks", labels=("Needs-Human",))]
    events = [_event(262, "issue.labels_changed", t, added=["needs-human"], removed=[])]

    staged = blocked_items_input(
        issues, lane=LANE, causes=(), ledger=(), case_files=None, timeline=events, cutoff=CUTOFF, coverage_proven=False
    )

    assert staged.items[0].blocking_labels[0].label == "Needs-Human"
    assert staged.items[0].blocked_since == t


def test_the_recorded_policy_rebuilds_the_engines_own_blocking_rule() -> None:
    """r4 F1: every configured name the label owner's blocking rule reads is
    recorded, so the audit's lane and the engine agree on every label."""
    from issue_orchestrator.control.label_manager import LabelManager
    from issue_orchestrator.infra.config import Config

    config = Config(label_prefix="bot", label_needs_human="needs-person", label_blocked="stuck")
    config.provider_resilience.circuit_breaker.label = "waiting-for-provider"
    engine = LabelManager(config)

    lane = BlockedLane.of(BlockedLane.policy_of(config))

    candidates = [
        "bot:needs-person", "bot:stuck", "waiting-for-provider", "bot:waiting-for-provider", "bot:blocked-failed",
        "bot:recovery-pending", "needs-human", "stuck", "bot:in-progress", "bot:blocked:claim-lost",
    ]
    assert [lane.labels.is_blocking(c) for c in candidates] == [engine.is_blocking(c) for c in candidates]
    assert any(engine.is_blocking(c) for c in ("waiting-for-provider", "bot:waiting-for-provider"))


def test_a_case_variant_of_a_blocked_pattern_label_is_blocking() -> None:
    """r6 F2: GitHub folds label case; a Blocked-* label still blocks (registered names, like blocked:claim-lost, the owner folds already)."""
    issues = [OpenIssueLabels(number=400, title="lost", labels=("Blocked-Upstream-Outage",))]

    staged = blocked_items_input(
        issues, lane=LANE, causes=(), ledger=(), case_files=None, timeline=(), cutoff=CUTOFF, coverage_proven=False
    )

    assert [b.label for b in staged.items[0].blocking_labels] == ["Blocked-Upstream-Outage"]
