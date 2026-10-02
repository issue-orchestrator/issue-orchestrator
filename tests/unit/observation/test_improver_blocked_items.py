"""Assembling ``blocked-items.json`` from records already read (#7490)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from issue_orchestrator.observation.improver_blocked_items import blocked_items_input
from issue_orchestrator.ports.engine_audit import OpenIssueLabels, TimelineEvent
from issue_orchestrator.ports.pending_work_claim_store import NeedsHumanCauseRow
from issue_orchestrator.ports.timeline_store import TimelineRecord

CUTOFF = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
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
        ISSUES, causes=(), ledger=(), case_files=None, timeline=(), cutoff=CUTOFF, coverage_proven=False
    )

    assert [i.number for i in staged.items] == [262, 400]
    assert [b.label for b in staged.items[1].blocking_labels] == ["blocked:claim-lost"]


def test_an_unread_source_is_named_and_its_facts_left_unknown() -> None:
    staged = blocked_items_input(
        ISSUES, causes="claim store unreadable: locked", ledger="tech-lead store absent",
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
        ISSUES, causes=rows, ledger=(), case_files=None, timeline=(), cutoff=CUTOFF, coverage_proven=False
    )

    assert [(c.cause, c.reason) for c in staged.items[0].needs_human_causes or ()] == [("agent_completion", "asked")]


def test_the_onset_is_the_last_put_on_that_was_never_taken_off() -> None:
    t0 = CUTOFF - timedelta(days=3)
    events = [
        _event(262, "issue.labels_changed", t0, added=["needs-human"], removed=[]),
        _event(262, "issue.labels_changed", t0 + timedelta(hours=1), added=[], removed=["needs-human"]),
        _event(262, "issue.needs_human", t0 + timedelta(hours=5), question="Split it?"),
        # Re-recorded while on: it never came off, so the onset stays.
        _event(262, "issue.labels_changed", t0 + timedelta(hours=6), added=["needs-human"], removed=[]),
        _event(262, "issue.labels_changed", t0 + timedelta(hours=7), added=["in-progress"], removed=[]),
    ]

    staged = blocked_items_input(
        ISSUES, causes=(), ledger=(), case_files=None, timeline=events, cutoff=CUTOFF, coverage_proven=False
    )

    item = staged.items[0]
    assert item.blocked_since == t0 + timedelta(hours=5)
    assert item.blocking_labels[0].since_event == "issue.needs_human"
    # Label changes that touch no blocking label are not block events.
    assert [e.event for e in item.block_events] == [
        "issue.labels_changed", "issue.labels_changed", "issue.needs_human", "issue.labels_changed",
    ]
    assert "question: Split it?" in item.block_events[2].detail
    assert item.timeline_coverage.from_ == t0


def test_the_engines_blocked_lane_decides_and_the_tech_leads_own_artefacts_are_not_items() -> None:
    issues = [
        OpenIssueLabels(number=10, title="held", labels=("recovery-pending",)),
        OpenIssueLabels(number=11, title="legacy", labels=("publish-failed",)),
        OpenIssueLabels(number=12, title="marker", labels=("tech-lead-needs-human",)),
        OpenIssueLabels(number=13, title="proposal", labels=("proposed-tech-lead",)),
        OpenIssueLabels(number=14, title="case file", labels=("tech-lead-observation",)),
    ]

    staged = blocked_items_input(
        issues, causes=(), ledger=(), case_files=None, timeline=(), cutoff=CUTOFF, coverage_proven=False
    )

    assert [i.number for i in staged.items] == [10, 11, 12]
