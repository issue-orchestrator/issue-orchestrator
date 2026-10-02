"""``blocked-items.json``: every blocked item of the audited engine (#7490).

The operator's objective is that blocked issues get resolved, so the improver
must measure the tech lead against it: for EVERY open issue that is blocked
(:class:`~.engine_audit.BlockedLane`), what blocks it, since when, what
cause the engine recorded, and what the tech lead decided or wrote about it.

The block is not the only thing stuck: work DOWNSTREAM of it can be too.
Each item names its open pull requests with their retained pipeline events,
and every ``refused_work`` anomaly of the audit about the item or one of its
PRs (porchpin #364, 2026-10-02: its PR #379 carried published validated work
whose review the engine queued and dropped on every scan while the issue was
blocked, and the improver graded only the block).

Pure assembly, like :mod:`.improver_inputs`: every argument is a record
already read through a read port from a snapshot, or the reason that source
could not be read. Absence of evidence stays visible: a source that could not
be read is named in its coverage block and its per-item facts are ``None``
or empty with that block saying why, never a guessed "nothing happened".
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from ..contracts.engine_audit import Anomaly, AnomalyKind, EngineAuditReport, RefusedWork
from ..contracts.improver_inputs import (
    BlockedItem,
    BlockedItemsInput,
    BlockEventInput,
    BlockingLabelInput,
    CaseFilesInput,
    Coverage,
    ItemPullRequestInput,
    NeedsHumanCauseInput,
    PipelineEventInput,
    StagedDecision,
    StalledWorkRef,
)
from ..control.review_scope import issues_of_pr
from ..domain.label_change_event import PRESENCE_UNKNOWN
from ..domain.improver_subjects import decision_issue, mentions_issue
from ..domain.tech_lead_charter_decisions import TechLeadCharterDecision
from ..events.catalog import EventName
from ..ports.engine_audit import OpenIssueLabels, TimelineEvent
from ..ports.pending_work_claim_store import NeedsHumanCauseRow
from ..ports.pull_request_tracker import PRInfo
from .engine_audit import BlockedLane
from .improver_inputs import applied_at, as_of, instant, ledger_coverage, staged_decision

#: How many of an item's most recent block events (and of each of its PRs'
#: pipeline events) are staged.
BLOCK_EVENTS_PER_ITEM = 20
#: An event's detail is cut to this many characters.
DETAIL_CHARS = 1500

BLOCKING_RULE = (
    "an open issue in the engine's blocked lane: a label its label owner classes as blocking"
    " (blocked, blocked-*, blocked:*, needs-human, recovery-pending, publish-failed, a provider"
    " outage, the legacy failed) or the tech-lead-needs-human marker; never a tech-lead"
    " proposal or case file"
)

#: Events about a block that carry no label change of their own.
_BLOCK_EVENTS = frozenset(
    {
        EventName.ISSUE_NEEDS_HUMAN.value,
        EventName.ISSUE_BLOCKED.value,
        EventName.ISSUE_UNBLOCKED.value,
        EventName.DEPENDENCY_BLOCKED.value,
        EventName.DEPENDENCY_UNBLOCKED.value,
    }
)
_LABELS_CHANGED = EventName.ISSUE_LABELS_CHANGED.value
#: Events whose detail is the labels they added and removed.
_LABEL_DIFFS = frozenset({_LABELS_CHANGED, EventName.PR_VIEW_CHANGED.value})


def blocked_items_input(
    issues: Sequence[OpenIssueLabels],
    *,
    prs: Sequence[PRInfo],
    audit: EngineAuditReport,
    lane: BlockedLane,
    causes: Sequence[NeedsHumanCauseRow] | str,
    ledger: Sequence[TechLeadCharterDecision] | str,
    case_files: CaseFilesInput | None,
    timeline: Iterable[TimelineEvent] | str,
    cutoff: datetime,
    coverage_proven: bool,
) -> BlockedItemsInput:
    """Every blocked item among ``issues`` (the audited repository's open
    issues, read at ``cutoff``), with its evidence.

    ``prs`` are the same repository's open pull requests, read with the
    issues by ``audit`` (of that repository): an item's open PRs come from
    them, and the refused work about it from the audit.

    ``causes``, ``ledger`` and ``timeline`` are the records read (the
    timeline holding the blocked items' own events, oldest first), or why
    the source could not be read. ``case_files`` is the staged
    ``case-files.json`` (None when it was not staged): items cite into it.
    """
    blocked = [issue for issue in sorted(issues, key=lambda i: i.number) if lane.blocking(issue.labels)]
    numbers = {issue.number for issue in blocked}
    by_issue: dict[int, list[TimelineEvent]] = {n: [] for n in numbers}
    if not isinstance(timeline, str):
        for event in timeline:
            if event.issue_number in by_issue:
                by_issue[event.issue_number].append(event)
    decisions = _decisions_about(ledger, numbers, cutoff)
    prs_of: dict[int, list[PRInfo]] = {n: [] for n in numbers}
    for pr in sorted(prs, key=lambda p: p.number):
        for number in issues_of_pr(pr, repo_slug=audit.repo) & numbers:
            prs_of[number].append(pr)
    refused = _Refusals(audit)
    return BlockedItemsInput(
        read_at=cutoff,
        blocking_rule=f"{BLOCKING_RULE}; labels read by {lane.source}",
        causes_coverage=Coverage(
            from_=None,
            to=cutoff,
            complete=False,
            detail=causes
            if isinstance(causes, str)
            else "the claim store's current rows, copied after the cutoff: a block a person"
            " put on by hand, or one recorded before causes were (#6999), has no row",
        ),
        decisions_coverage=_decisions_coverage(ledger, cutoff, proven=coverage_proven),
        items=tuple(
            _item(
                issue,
                lane=lane,
                causes=None if isinstance(causes, str) else causes,
                decisions=decisions.get(issue.number, ()),
                case_files=case_files,
                events=timeline if isinstance(timeline, str) else by_issue[issue.number],
                prs=prs_of[issue.number],
                refused=refused,
                cutoff=cutoff,
            )
            for issue in blocked
        ),
    )


def _item(
    issue: OpenIssueLabels,
    *,
    lane: BlockedLane,
    causes: Sequence[NeedsHumanCauseRow] | None,
    decisions: tuple[StagedDecision, ...],
    case_files: CaseFilesInput | None,
    events: Sequence[TimelineEvent] | str,
    prs: Sequence[PRInfo],
    refused: "_Refusals",
    cutoff: datetime,
) -> BlockedItem:
    labels = lane.blocking(issue.labels)
    since = {} if isinstance(events, str) else _label_onsets(events, lane)
    blocking = tuple(
        BlockingLabelInput(
            label=label,
            since_at=since[label.casefold()][0] if label.casefold() in since else None,
            since_event=since[label.casefold()][1] if label.casefold() in since else None,
        )
        for label in labels
    )
    known = [b.since_at for b in blocking if b.since_at is not None]
    return BlockedItem(
        number=issue.number,
        title=issue.title,
        labels=tuple(sorted(issue.labels)),
        blocking_labels=blocking,
        blocked_since=min(known) if len(known) == len(blocking) else None,
        needs_human_causes=None
        if causes is None
        else tuple(
            NeedsHumanCauseInput(cause=row.cause, reason=row.reason)
            for row in sorted(causes, key=lambda r: r.cause)
            if row.issue_number == issue.number
        ),
        block_events=() if isinstance(events, str) else _block_events(events, lane),
        timeline_coverage=_timeline_coverage(events, cutoff),
        decisions=decisions,
        case_file_ids=()
        if case_files is None
        else tuple(c.id for c in case_files.case_files if mentions_issue(c.body, {issue.number})),
        diagnosis_ids=()
        if case_files is None
        else tuple(
            d.id
            for d in case_files.diagnoses
            if d.subject_issue_number == issue.number or mentions_issue(d.body, {issue.number})
        ),
        open_prs=tuple(_pull_request(pr, events) for pr in prs),
        stalled_work=refused.about(issue.number, prs),
    )


def _pull_request(pr: PRInfo, events: Sequence[TimelineEvent] | str) -> ItemPullRequestInput:
    if pr.draft is None:
        raise ValueError(f"open PR #{pr.number} did not report whether it is a draft")
    own = [] if isinstance(events, str) else [e for e in events if _pr_number(e) == pr.number]
    staged = own[-BLOCK_EVENTS_PER_ITEM:]
    return ItemPullRequestInput(
        number=pr.number,
        draft=pr.draft,
        pipeline_events=tuple(
            PipelineEventInput(at=instant(e.record.timestamp), event=_name(e), detail=_detail(e)) for e in staged
        ),
        last_event_at=instant(staged[-1].record.timestamp) if staged else None,
    )


def _pr_number(event: TimelineEvent) -> int | None:
    value = event.record.data.get("pr_number")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return int(value) if isinstance(value, str) and value.isdigit() else None


class _Refusals:
    """The audit's refused-work anomalies, with the subjects each one names."""

    def __init__(self, audit: EngineAuditReport) -> None:
        records: dict[tuple[str, str], RefusedWork] = {
            (r.subject, r.reason): r for r in audit.no_progress.refused_work
        }
        self._anomalies: list[tuple[Anomaly, frozenset[str]]] = [
            (a, frozenset(records[(a.subject, a.signature)].related))
            for a in audit.anomalies
            if a.kind is AnomalyKind.REFUSED_WORK
        ]

    def about(self, number: int, prs: Sequence[PRInfo]) -> tuple[StalledWorkRef, ...]:
        """The refused work downstream of item ``number``: work of the item
        or of one of its open PRs, or work whose refusal names the item (the
        engine refusing a PR's review because of THIS issue's block, however
        the PR links to it)."""
        item = f"#{number}"
        subjects = {item, *(f"PR #{pr.number}" for pr in prs)}
        return tuple(
            StalledWorkRef(kind=a.kind.value, subject=a.subject, signature=a.signature, detail=a.detail)
            for a, related in self._anomalies
            if a.subject in subjects or item in related
        )


def _label_onsets(events: Sequence[TimelineEvent], lane: BlockedLane) -> dict[str, tuple[datetime, str]]:
    """For every blocking label the retained events leave ON, keyed by its
    casefolded name (GitHub folds label case): when and by which event it was
    last put on.

    A recorded label change is the engine's own diff (it adds only a label
    it read as absent), so an add is a fresh onset, even with no removal
    recorded before it: a person may have taken the label off on GitHub,
    which the timeline never sees. An add whose presence read failed
    (``presence_unknown``) may have found the label already on, or put it
    back after a removal the timeline never saw: the onset is unknown from it
    until a certain add. A removal forgets the label. A
    needs-human request (``issue.needs_human``) is never an onset: it is
    emitted also for a label already on (the tech lead's escalation
    reconciler re-asserts an existing block), so it is a block event only."""
    on: dict[str, tuple[datetime, str]] = {}
    for event in (e for e in events if _name(e) == _LABELS_CHANGED):
        data = event.record.data
        for label in _labels(data.get("removed")):
            on.pop(label.casefold(), None)
        uncertain = data.get(PRESENCE_UNKNOWN) is True
        for label in (b for b in _labels(data.get("added")) if lane.is_blocking(b)):
            if uncertain:
                # It may have found the label on (the known onset stands) or
                # put it back after an unrecorded removal (a new onset): which
                # is unknown, so the onset is too.
                on.pop(label.casefold(), None)
            else:
                on[label.casefold()] = (instant(event.record.timestamp), _LABELS_CHANGED)
    return on


def _block_events(events: Sequence[TimelineEvent], lane: BlockedLane) -> tuple[BlockEventInput, ...]:
    about = [
        BlockEventInput(at=instant(e.record.timestamp), event=_name(e), detail=_detail(e))
        for e in events
        if _name(e) in _BLOCK_EVENTS
        or (
            _name(e) == _LABELS_CHANGED
            and any(
                lane.is_blocking(label)
                for label in (*_labels(e.record.data.get("added")), *_labels(e.record.data.get("removed")))
            )
        )
    ]
    return tuple(about[-BLOCK_EVENTS_PER_ITEM:])


def _detail(event: TimelineEvent) -> str:
    data = event.record.data
    if _name(event) in _LABEL_DIFFS:
        text = f"added {list(_labels(data.get('added')))} removed {list(_labels(data.get('removed')))}"
    else:
        parts = [
            f"{key}: {data[key]}"
            for key in ("question", "reason", "summary", "blocked_by")
            if data.get(key) not in (None, "", [])
        ]
        text = "; ".join(parts) or str(data.get("narrative", ""))
    return text if len(text) <= DETAIL_CHARS else text[: DETAIL_CHARS - 1] + "…"


def _timeline_coverage(events: Sequence[TimelineEvent] | str, cutoff: datetime) -> Coverage:
    if isinstance(events, str):
        return Coverage(from_=None, to=cutoff, complete=False, detail=f"timeline unread: {events}")
    if not events:
        return Coverage(
            from_=None, to=cutoff, complete=False,
            detail="no retained event: the timeline trims each issue's oldest rows",
        )
    return Coverage(
        from_=instant(events[0].record.timestamp),
        to=cutoff,
        complete=False,
        detail="the issue's retained events: the timeline trims each issue's oldest rows and"
        " its writer is best-effort, so an earlier or unrecorded change is not shown",
    )


def _decisions_about(
    ledger: Sequence[TechLeadCharterDecision] | str, numbers: set[int], cutoff: datetime
) -> dict[int, tuple[StagedDecision, ...]]:
    if isinstance(ledger, str):
        return {}
    found: dict[int, list[StagedDecision]] = {}
    for recorded in ledger:
        issue = decision_issue(recorded.target_number, recorded.anchor_issue_number)
        decided = instant(recorded.decided_at)
        if issue not in numbers or decided > cutoff:
            continue
        decision = as_of(recorded, cutoff)
        found.setdefault(issue, []).append(staged_decision(decision, decided, applied_at(decision)))
    return {n: tuple(ds) for n, ds in found.items()}


def _decisions_coverage(
    ledger: Sequence[TechLeadCharterDecision] | str, cutoff: datetime, *, proven: bool
) -> Coverage:
    if isinstance(ledger, str):
        return Coverage(from_=None, to=cutoff, complete=False, detail=f"charter ledger unread: {ledger}")
    earliest = instant(ledger[0].decided_at) if ledger else None
    return ledger_coverage(
        earliest,
        window_start=earliest if earliest is not None else cutoff,
        cutoff=cutoff,
        what="the whole charter ledger",
        empty="the ledger holds no decision, so it cannot show when it began recording",
        proven=proven,
    )


def _name(event: TimelineEvent) -> str:
    return event.record.source_event or event.record.event


def _labels(value: Any) -> tuple[str, ...]:
    return tuple(str(v) for v in value) if isinstance(value, list) else ()


__all__ = ["BLOCKING_RULE", "BLOCK_EVENTS_PER_ITEM", "blocked_items_input"]
