"""Assembling ``blocked-items.json`` from records already read (#7490)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from issue_orchestrator.contracts.engine_audit import (
    Anomaly,
    AnomalyKind,
    AuditSource,
    EngineAuditReport,
    NoProgressSection,
    RefusedWork,
)
from issue_orchestrator.observation.engine_audit import BlockedLane
from issue_orchestrator.observation.improver_blocked_items import blocked_items_input
from issue_orchestrator.ports.engine_audit import OpenIssueLabels, TimelineEvent
from issue_orchestrator.ports.pending_work_claim_store import NeedsHumanCauseRow
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.ports.timeline_store import TimelineRecord

CUTOFF = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
LANE = BlockedLane.of(None)
REPO = "porchpin/porchpin"
ISSUES = [
    OpenIssueLabels(number=1, title="not blocked", labels=("bug", "in-progress")),
    OpenIssueLabels(number=262, title="asks", labels=("needs-human",)),
    OpenIssueLabels(number=400, title="failed", labels=("blocked:claim-lost",)),
]


def _refusal(subject: str, *related: str) -> RefusedWork:
    return RefusedWork(subject=subject, related=related, action="review", reason="issue_blocked", loggers=("scanner",),
                       example="Skipping", count=9, since_state_change=9,
                       first_seen=CUTOFF.isoformat(), last_seen=CUTOFF.isoformat())


def _audit(*refusals: RefusedWork) -> EngineAuditReport:
    """An audit of ``REPO`` whose only anomalies are ``refusals``."""
    return EngineAuditReport(
        generated_at=CUTOFF.isoformat(), repo=REPO, state_dir="/state", partial=False, sources=(),
        validated_work=None, action_liveness=None, tech_lead=None, claims=None, github=None,
        no_progress=NoProgressSection(
            window_start=CUTOFF.isoformat(), window_end=CUTOFF.isoformat(), log=None, log_signatures=(),
            refused_work=refusals, timeline_repeats=(), state_changes=(),
        ),
        fetch_cost=None,
        anomalies=tuple(
            Anomaly(kind=AnomalyKind.REFUSED_WORK, sources=(AuditSource.LOG, AuditSource.TIMELINE),
                    subject=r.subject, signature=r.signature, detail="9 refusal(s)", count=9)
            for r in refusals
        ),
    )


def _event(issue: int, name: str, at: datetime, **data: object) -> TimelineEvent:
    return TimelineEvent(
        issue_number=issue,
        record=TimelineRecord(event_id=f"{name}-{at}", timestamp=at.isoformat(), event=name, data=dict(data),
                              source_event=name),
    )


def test_only_blocked_issues_are_items_and_every_blocking_label_counts() -> None:
    staged = blocked_items_input(
        ISSUES, prs=(), audit=_audit(), lane=LANE, causes=(), ledger=(), case_files=None, timeline=(), cutoff=CUTOFF, coverage_proven=False
    )

    assert [i.number for i in staged.items] == [262, 400]
    assert [b.label for b in staged.items[1].blocking_labels] == ["blocked:claim-lost"]


def test_an_unread_source_is_named_and_its_facts_left_unknown() -> None:
    staged = blocked_items_input(
        ISSUES, prs=(), audit=_audit(), lane=LANE, causes="claim store unreadable: locked", ledger="tech-lead store absent",
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
        ISSUES, prs=(), audit=_audit(), lane=LANE, causes=rows, ledger=(), case_files=None, timeline=(), cutoff=CUTOFF, coverage_proven=False
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
        ISSUES, prs=(), audit=_audit(), lane=LANE, causes=(), ledger=(), case_files=None, timeline=events, cutoff=CUTOFF, coverage_proven=False
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
        ISSUES, prs=(), audit=_audit(), lane=LANE, causes=(), ledger=(), case_files=None, timeline=events, cutoff=CUTOFF, coverage_proven=False
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
        ISSUES, prs=(), audit=_audit(), lane=LANE, causes=(), ledger=(), case_files=None, timeline=events, cutoff=CUTOFF, coverage_proven=False
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
        issues, prs=(), audit=_audit(), lane=LANE, causes=(), ledger=(), case_files=None, timeline=(), cutoff=CUTOFF, coverage_proven=False
    )

    assert [i.number for i in staged.items] == [10, 11, 12]


def test_an_add_that_could_not_read_presence_makes_the_onset_unknown() -> None:
    """r1 F4, revised by r7 F1: the applier records an add even when its
    presence read failed. The label may already have been on (the earlier
    onset) or been taken off by hand and put back (a new one): unknown."""
    t0 = CUTOFF - timedelta(days=2)
    events = [
        _event(262, "issue.labels_changed", t0, added=["needs-human"], removed=[]),
        _event(262, "issue.labels_changed", t0 + timedelta(days=1), added=["needs-human"], removed=[],
               presence_unknown=True),
    ]

    staged = blocked_items_input(
        ISSUES, prs=(), audit=_audit(), lane=LANE, causes=(), ledger=(), case_files=None, timeline=events, cutoff=CUTOFF, coverage_proven=False
    )

    assert staged.items[0].blocked_since is None


def test_an_add_that_could_not_read_presence_is_no_onset() -> None:
    """r2 F2: with no earlier onset retained, an uncertain add leaves it unknown."""
    events = [
        _event(262, "issue.labels_changed", CUTOFF - timedelta(hours=3), added=["needs-human"], removed=[],
               presence_unknown=True),
    ]

    staged = blocked_items_input(
        ISSUES, prs=(), audit=_audit(), lane=LANE, causes=(), ledger=(), case_files=None, timeline=events, cutoff=CUTOFF, coverage_proven=False
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
        ISSUES, prs=(), audit=_audit(), lane=LANE, causes=(), ledger=(), case_files=None, timeline=events, cutoff=CUTOFF, coverage_proven=False
    )

    assert staged.items[0].blocked_since is None


def test_a_label_github_returns_in_another_case_keeps_its_onset() -> None:
    """r4 F3: GitHub folds label case; the engine recorded needs-human."""
    t = CUTOFF - timedelta(hours=3)
    issues = [OpenIssueLabels(number=262, title="asks", labels=("Needs-Human",))]
    events = [_event(262, "issue.labels_changed", t, added=["needs-human"], removed=[])]

    staged = blocked_items_input(
        issues, prs=(), audit=_audit(), lane=LANE, causes=(), ledger=(), case_files=None, timeline=events, cutoff=CUTOFF, coverage_proven=False
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
        issues, prs=(), audit=_audit(), lane=LANE, causes=(), ledger=(), case_files=None, timeline=(), cutoff=CUTOFF, coverage_proven=False
    )

    assert [b.label for b in staged.items[0].blocking_labels] == ["Blocked-Upstream-Outage"]


# -- downstream of the block ---------------------------------------------------


def _pr(number: int, *, branch: str, body: str = "", draft: bool | None = False) -> PRInfo:
    return PRInfo(number=number, title="t", url=f"https://x/{number}", branch=branch, body=body,
                  state="open", labels=[], draft=draft)


def test_an_items_open_prs_carry_their_pipeline_and_the_work_the_engine_refuses() -> None:
    """porchpin #364: its PR #379 queued for review and dropped, every scan."""
    t0 = CUTOFF - timedelta(hours=1)
    events = [
        _event(262, "issue.labels_changed", t0, added=["needs-human"], removed=[]),
        _event(262, "pr.view_changed", t0 + timedelta(minutes=1), pr_number=379,
               added=["needs-code-review"], removed=[]),
        _event(262, "review.queued", t0 + timedelta(minutes=2), pr_number=379),
        _event(262, "review.skipped", t0 + timedelta(minutes=3), pr_number=379,
               reason="stale_pending_review:issue_blocked"),
        _event(262, "review.queued", t0 + timedelta(minutes=4), pr_number=12),
    ]
    prs = [
        _pr(379, branch="262-split-the-index"),
        _pr(380, branch="feature", body="Refs #262", draft=True),
        _pr(381, branch="400-other"),
        _pr(382, branch="feature", body="mentions #262 only"),
    ]
    audit = _audit(
        _refusal("PR #379", "#262"), _refusal("PR #381", "#400"), _refusal("#262"),
        # A PR neither its branch nor its body links: the refusal names the issue.
        _refusal("PR #900", "#262"),
    )

    staged = blocked_items_input(
        ISSUES, prs=prs, audit=audit, lane=LANE, causes=(), ledger=(), case_files=None,
        timeline=events, cutoff=CUTOFF, coverage_proven=False,
    )

    item = staged.items[0]
    assert [(p.number, p.draft) for p in item.open_prs] == [(379, False), (380, True)]
    pipeline = item.open_prs[0].pipeline_events
    assert [e.event for e in pipeline] == ["pr.view_changed", "review.queued", "review.skipped"]
    assert pipeline[0].detail == "added ['needs-code-review'] removed []"
    assert pipeline[2].detail == "reason: stale_pending_review:issue_blocked"
    assert [e.reason for e in pipeline] == [None, None, "stale_pending_review:issue_blocked"]
    assert item.open_prs[0].last_event_at == t0 + timedelta(minutes=3)
    assert item.open_prs[1].pipeline_events == () and item.open_prs[1].last_event_at is None
    assert [(w.kind, w.subject, w.signature) for w in item.stalled_work] == [
        ("refused_work", "PR #379", "review:issue_blocked"), ("refused_work", "#262", "review:issue_blocked"),
        ("refused_work", "PR #900", "review:issue_blocked"),
    ]
    # #400's own PR #381 is its downstream work, never #262's.
    assert [w.subject for w in staged.items[1].stalled_work] == ["PR #381"]


def test_an_open_pr_that_does_not_say_whether_it_is_a_draft_is_refused() -> None:
    with pytest.raises(ValueError, match="#379"):
        blocked_items_input(
            ISSUES, prs=[_pr(379, branch="262-x", draft=None)], audit=_audit(), lane=LANE, causes=(),
            ledger=(), case_files=None, timeline=(), cutoff=CUTOFF, coverage_proven=False,
        )
