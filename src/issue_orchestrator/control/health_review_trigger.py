"""Periodic health-review trigger (ADR-0031 §4).

Single owner for the trigger side of the health-review lifecycle:

- **classification**: split the fact gatherer's one open-anchor scan into
  batch vs health-review anchors (the ``HEALTH_REVIEW_MARKER_LABEL`` is the
  crash-safe dedup key, ADR-0013 — no extra GitHub call);
- **due policy**: the interval math against ``state.last_health_review_at``;
- **planning**: the anchor-issue creation action when the review is due and
  no open anchor or pending launch already covers it;
- **intake**: after a successful creation, route the anchor into the pending
  queue through the owning :class:`PendingSessionQueues` operation for the
  variant the marker label declares (batch vs health — #6768 round 3 typed
  intake), and stamp/persist ``state.last_health_review_at`` so neither the
  next tick nor a restart double-fires.

The anchor issue then rides the existing batch-issue lifecycle: it is picked
up like any triage-agent issue, the launcher derives the HEALTH_REVIEW flavor
from the marker label, and completion closes the anchor when a valid decision
pair lands (see ``triage_session_policy`` / ``triage_completion``).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Iterable, Optional, Sequence

from ..domain.triage_session import HEALTH_REVIEW_MARKER_LABEL, TriageSessionFlavor
from .actions import CreateTriageIssueAction

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, PendingTriageReview, TriageFacts
    from ..infra.config import Config
    from ..ports import Issue
    from ..ports.queue_cache_store import QueueCacheStore
    from .session_routing import TriageQueueOutcome

logger = logging.getLogger(__name__)

HEALTH_REVIEW_ISSUE_TITLE = "Health Review — walk the floor"

_HEALTH_REVIEW_ISSUE_BODY = """## Periodic Health Review (ADR-0031 §4)

Walk the floor: review the orchestrator board holistically instead of
auditing a PR batch. Your session receives a board snapshot
(`triage-data/board-snapshot.json`) with active sessions, pending/blocked
queues, recent failures, timeline extracts, and an orchestrator log tail.

Look for hung or aging sessions, queue pile-ups, repeated failures, and
cross-job patterns. Report findings and propose actions through the triage
decision artifact; the orchestrator closes this issue when the review lands.
"""


def health_review_interval_minutes(config: "Config") -> int:
    """Effective health-review interval; 0 when disabled or no triage agent."""
    if not config.triage_review_agent:
        return 0
    return config.triage.health_review.interval_minutes


def health_review_due(
    config: "Config", state: "OrchestratorState", now: float
) -> bool:
    """True when the configured interval has elapsed since the last review."""
    interval_minutes = health_review_interval_minutes(config)
    if interval_minutes <= 0:
        return False
    return now - state.last_health_review_at >= interval_minutes * 60


def classify_triage_anchor_issues(
    issues: Iterable["Issue"], filter_label: Optional[str]
) -> tuple[Optional[int], Optional[int]]:
    """Classify open triage-agent issues into (batch, health_review) anchors.

    One pass over the fact gatherer's existing ``list_issues`` scan — the
    marker label identifies health-review anchors, the historical title
    match identifies batch anchors. Issues outside the active filter-label
    scope are ignored (mirrors the pre-existing batch behavior).
    """
    batch: Optional[int] = None
    health: Optional[int] = None
    for issue in issues:
        if filter_label and filter_label not in issue.labels:
            continue
        if HEALTH_REVIEW_MARKER_LABEL in issue.labels:
            health = issue.number if health is None else health
            continue
        if batch is None and (
            "Batch Review" in issue.title or "Triage Review" in issue.title
        ):
            batch = issue.number
    return batch, health


def plan_health_review_issue_creation(
    facts: "Optional[TriageFacts]",
    pending_triage: Sequence["PendingTriageReview"],
    config: "Config",
) -> Optional[CreateTriageIssueAction]:
    """Plan the health-review anchor creation when due and not duplicated.

    Dedup layers: the open marker-labeled anchor (GitHub, crash-safe), the
    pending-launch queue (covers the window before the label scan refreshes;
    queue items carry a typed flavor, #6768 B5), and
    ``state.last_health_review_at`` (already folded into
    ``facts.health_review_due``).
    """
    if facts is None or not facts.health_review_due:
        return None
    if facts.existing_health_review_issue is not None:
        logger.debug(
            "Planner: health-review anchor #%d already open",
            facts.existing_health_review_issue,
        )
        return None
    if any(
        pending.flavor is TriageSessionFlavor.HEALTH_REVIEW
        for pending in pending_triage
    ):
        logger.debug("Planner: health review already pending launch")
        return None
    labels = tuple(
        value
        for value in (
            config.triage_review_agent,
            config.filtering.label,
            HEALTH_REVIEW_MARKER_LABEL,
        )
        if value
    )
    logger.info("Planner: creating health-review anchor issue (labels=%s)", labels)
    return CreateTriageIssueAction(
        title=HEALTH_REVIEW_ISSUE_TITLE,
        body=_HEALTH_REVIEW_ISSUE_BODY,
        labels=labels,
        pr_count=0,
        reason="health review interval elapsed",
    )


def _queue_anchor_by_marker(
    state: "OrchestratorState", issue_number: int, title: str, labels: Iterable[str]
) -> "TriageQueueOutcome":
    """Route an orchestrator-created anchor to its variant's owner queue op.

    The marker label declares the variant (labels are the crash-safe truth
    the launcher later re-derives the flavor from), so both creation intake
    and startup recovery pick the matching operation on
    :class:`PendingSessionQueues` instead of overloading batch intake.
    """
    from .session_routing import PendingSessionQueues

    queues = PendingSessionQueues(state)
    if HEALTH_REVIEW_MARKER_LABEL in labels:
        return queues.queue_health_review(issue_number, title)
    return queues.queue_batch_review(issue_number, title)


def intake_created_triage_anchor(
    action: CreateTriageIssueAction,
    issue_number: int,
    state: "OrchestratorState",
    store: "Optional[QueueCacheStore]",
) -> "TriageQueueOutcome":
    """Route a successfully created triage anchor into the pending queue.

    Health creations additionally stamp/persist ``last_health_review_at``
    (:func:`record_health_review_creation` keys off the marker label).
    """
    outcome = _queue_anchor_by_marker(state, issue_number, action.title, action.labels)
    record_health_review_creation(action, state, store)
    return outcome


def queue_recovered_triage_anchor(
    state: "OrchestratorState", issue: "Issue"
) -> "TriageQueueOutcome":
    """Route a recovered open anchor into the pending queue (startup).

    Same marker rule as creation intake: the queued flavor is forwarded
    verbatim to launch (#6768 B5), so recovering a marker-labeled anchor as
    BATCH_REVIEW would relaunch it as a batch audit — manifest prep, batch
    authority, manifest labels on completion. No timestamp stamping: the
    anchor already exists; ``last_health_review_at`` records creation time.
    """
    return _queue_anchor_by_marker(state, issue.number, issue.title, issue.labels)


def record_health_review_creation(
    action: CreateTriageIssueAction,
    state: "OrchestratorState",
    store: "Optional[QueueCacheStore]",
    now: Optional[float] = None,
) -> None:
    """Record a successful anchor creation (marker-labeled actions only).

    Stamps the in-memory state (stops the next tick re-firing) and persists
    the marker durably (stops a restart re-firing). Persistence failure is
    logged, not raised: the in-memory stamp plus the open anchor's marker
    label still guard the current process, and the created issue must not be
    reported as an apply failure.
    """
    if HEALTH_REVIEW_MARKER_LABEL not in action.labels:
        return
    stamped_at = time.time() if now is None else now
    state.last_health_review_at = stamped_at
    if store is None:
        return
    try:
        store.save_last_health_review_at(stamped_at)
    except Exception:
        logger.warning(
            "Failed to persist last_health_review_at; restart may re-fire early",
            exc_info=True,
        )
