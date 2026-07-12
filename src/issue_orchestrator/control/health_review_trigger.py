"""Periodic health-review trigger (ADR-0031 §4).

Single owner for the trigger side of the health-review lifecycle:

- **anchor discovery**: the scoped, exhaustive open-anchor scans shared by
  fact gathering and startup recovery (one eligibility rule for both paths),
  plus the marker-scoped dedup lookup (the ``HEALTH_REVIEW_MARKER_LABEL`` is
  the crash-safe dedup key, ADR-0013);
- **due policy**: the interval math against ``state.last_health_review_at``;
- **planning**: the anchor-issue creation action when the review is due, no
  open anchor or pending launch already covers it, and the owned
  paused/capacity gate (``TriageWorkflow``) says go — anchor shaping (labels,
  priority title, milestone intent) comes from the ``triage_issue_policy``
  owner, same as batch anchors;
- **intake**: after a successful creation, route the anchor into the pending
  queue through the owning :class:`PendingSessionQueues` operation for the
  variant the marker label declares (batch vs health — #6768 round 3 typed
  intake), and stamp/persist ``state.last_health_review_at`` so neither the
  next tick nor a restart double-fires;
- **restart reconciliation**: hydrate ``last_health_review_at`` from the
  durable store AND the newest marker-labeled anchor issue — the issues are
  the crash-safe truth (ADR-0013), so a store persist failure can never
  re-fire the review before the interval elapses.

The anchor issue then rides the existing batch-issue lifecycle: it is picked
up like any triage-agent issue, the launcher derives the HEALTH_REVIEW flavor
from the marker label, and completion closes the anchor when a valid decision
pair lands (see ``triage_session_policy`` / ``triage_completion``).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Iterable, Optional, Sequence

from ..domain.triage_session import HEALTH_REVIEW_MARKER_LABEL, TriageSessionFlavor
from .actions import CreateTriageIssueAction
from .triage_issue_policy import (
    apply_triage_priority_prefix,
    health_review_issue_labels,
    triage_issue_milestone_intent,
)

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, PendingTriageReview, TriageFacts
    from ..infra.config import Config
    from ..ports import Issue, RepositoryHost
    from ..ports.queue_cache_store import QueueCacheStore
    from ..ports.triage_authority import TriageAuthorityStore
    from .session_routing import TriageQueueOutcome
    from .workflows import TriageWorkflow

logger = logging.getLogger(__name__)

HEALTH_REVIEW_ISSUE_TITLE = "Health Review — walk the floor"

# Marker-scoped anchor lookups (health-review dedup / last-fired) match at
# most a handful of issues, so a single 100-item page (the GitHub API page
# maximum) is exhaustive for them. The BROAD triage-agent scan instead uses
# the paginated ``TRIAGE_PROPOSAL_SCAN_LIMIT`` (#6779 R4), because gated
# proposals share the triage-agent label and could crowd an older anchor past
# a fixed first page (#6763 findings 4 and 7).
_ANCHOR_SCAN_LIMIT = 100

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


def has_health_review_marker(labels: Iterable[str]) -> bool:
    """True when the ADR-0031 §4 marker label is present.

    Casefolded throughout — GitHub label names are case-insensitive, and the
    marker is crash-safe truth (flavor derivation, dedup, recovery routing).
    """
    marker = HEALTH_REVIEW_MARKER_LABEL.casefold()
    return any(name.casefold() == marker for name in labels)


def _scoped_issues(
    issues: Iterable["Issue"], filter_label: Optional[str]
) -> list["Issue"]:
    """Enforce the ``filtering.label`` scope client-side (casefolded).

    The scope label is also part of the server-side query, but adapters and
    fakes may not filter; eligibility is a policy rule and must hold
    regardless of transport behavior.
    """
    folded = (filter_label or "").casefold()
    return [
        issue
        for issue in issues
        if not folded or any(name.casefold() == folded for name in issue.labels)
    ]


def _anchor_query_labels(config: "Config", *extra: str) -> list[str]:
    """Server-side label scope for anchor queries (agent + filter + extras)."""
    if not config.triage_review_agent:
        raise ValueError(
            "triage anchor discovery requires a configured triage_review_agent"
        )
    return [
        value
        for value in (config.triage_review_agent, config.filtering.label, *extra)
        if value
    ]


def discover_open_triage_anchor_issues(
    repository_host: "RepositoryHost", config: "Config"
) -> list["Issue"]:
    """Scoped, exhaustive discovery of open triage anchor issues.

    Single owner for anchor eligibility, shared by fact gathering and startup
    recovery (#6763 finding 7). Both paths must apply the same
    ``filtering.label`` scope — startup queueing an out-of-scope anchor would
    let a run-scoped restart launch another run's health review — and both
    must page the COMPLETE open set: gated proposals share the triage-agent
    label (#6778), so a proposal backlog could otherwise push an older anchor
    (or approved op) past a small window. The exhaustive scan bound is the
    shared ``TRIAGE_PROPOSAL_SCAN_LIMIT`` (#6779 R4, paginated), so the SAME
    open scan feeds both anchor classification and proposal reconciliation.
    """
    from .triage_proposals import TRIAGE_PROPOSAL_SCAN_LIMIT

    issues = repository_host.list_issues(
        labels=_anchor_query_labels(config),
        state="open",
        limit=TRIAGE_PROPOSAL_SCAN_LIMIT,
    )
    return _scoped_issues(issues, config.filtering.label)


def discover_open_health_review_anchor(
    repository_host: "RepositoryHost", config: "Config"
) -> Optional[int]:
    """Marker-scoped open health-anchor lookup (crash-safe dedup, finding 4).

    Scoping the query on the marker label itself makes the lookup exhaustive
    even when the broader triage-agent scan is crowded past its page size.
    Callers invoke this only while a creation decision is actually pending
    (GitHub API discipline — see ``FactGatherer.gather_triage_facts``).
    """
    issues = repository_host.list_issues(
        labels=_anchor_query_labels(config, HEALTH_REVIEW_MARKER_LABEL),
        state="open",
        limit=_ANCHOR_SCAN_LIMIT,
    )
    scoped = [
        issue
        for issue in _scoped_issues(issues, config.filtering.label)
        if has_health_review_marker(issue.labels)
    ]
    return scoped[0].number if scoped else None


def classify_triage_anchor_issues(
    issues: Iterable["Issue"], filter_label: Optional[str]
) -> tuple[Optional[int], Optional[int]]:
    """Classify open triage-agent issues into (batch, health_review) anchors.

    One pass over the shared anchor discovery scan — the marker label
    identifies health-review anchors, the historical title match identifies
    batch anchors. Issues outside the active filter-label scope are ignored
    (belt and braces: the discovery owner already enforces scope). All label
    comparisons casefold — GitHub label names are case-insensitive.
    """
    batch: Optional[int] = None
    health: Optional[int] = None
    for issue in _scoped_issues(issues, filter_label):
        if has_health_review_marker(issue.labels):
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
    *,
    workflow: "TriageWorkflow",
    active_session_count: int,
    paused: bool,
) -> Optional[CreateTriageIssueAction]:
    """Plan the health-review anchor creation when due, not duplicated, and
    allowed by the owned paused/capacity gate.

    Dedup layers: the open marker-labeled anchor (GitHub, crash-safe), the
    pending-launch queue (covers the window before the label scan refreshes;
    queue items carry a typed flavor, #6768 B5), and
    ``state.last_health_review_at`` (already folded into
    ``facts.health_review_due``). The gate runs last so TRIAGE_SKIPPED is
    only emitted when a creation would otherwise happen; due-ness persists
    (no stamp), so creation retries once the gate opens.

    Anchor shaping (labels including the marker, configured priority title,
    milestone intent) comes from the ``triage_issue_policy`` owner — the same
    policy batch anchors get (#6763 finding 5).
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
    if not workflow.should_create_health_review(
        active_session_count=active_session_count, paused=paused
    ):
        logger.debug("Planner: health review due but gated (paused/at capacity)")
        return None
    labels = health_review_issue_labels(config)
    title = apply_triage_priority_prefix(config, HEALTH_REVIEW_ISSUE_TITLE)
    # Milestone travels as INTENT; the applier resolves an explicit name at
    # the create-issue execution boundary (#6769 finding 4). Health anchors
    # have no source PRs, so only the explicit strategy can apply.
    milestone = triage_issue_milestone_intent(config, ())
    logger.info("Planner: creating health-review anchor issue (labels=%s)", labels)
    return CreateTriageIssueAction(
        title=title,
        body=_HEALTH_REVIEW_ISSUE_BODY,
        labels=labels,
        pr_count=0,
        milestone=milestone,
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
    if has_health_review_marker(labels):
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


def recover_pending_triage_anchors(
    state: "OrchestratorState",
    *,
    repository_host: "RepositoryHost",
    config: "Config",
    session_exists: Callable[[str], bool],
    triage_authority: "TriageAuthorityStore | None",
) -> None:
    """Requeue open triage anchors on startup (crash-safe label recovery).

    Gated triage proposals share the triage agent label (#6778) but are
    never anchors: gate-labeled issues await operator approval, and
    op-backed issues without the gate label are approved ops the planner
    executes from the fact scan. Requeuing either as a batch anchor would
    launch a triage session on a proposal issue — the same
    ``reconcile_triage_proposals`` owner the fact gatherer uses excludes
    them here, over an EXHAUSTIVE open scan (#6779 R4) so a proposal backlog
    can never hide an anchor. Startup recovery is read-only (#6779 R10):
    leaked/terminal ledger rows are NOT discarded here — the first control
    tick's fact gatherer surfaces them as cleanup candidates and the planner's
    confirm-and-discard action self-heals them.
    """
    from .session_routing import TriageQueueOutcome
    from .triage_proposals import reconcile_triage_proposals

    assert config.triage_review_agent is not None  # caller-gated
    # Shared scoped/exhaustive anchor discovery (#6763 finding 7) feeds the
    # proposal reconciliation (#6779 R2/R4): the SAME exhaustive open scan the
    # fact gatherer classifies, so startup applies the same filtering.label
    # eligibility rule and a proposal backlog can never hide an anchor.
    issues = discover_open_triage_anchor_issues(repository_host, config)
    ops = (
        dict(triage_authority.list_ops())
        if triage_authority is not None
        else {}
    )
    reconciled = reconcile_triage_proposals(issues, ops=ops)
    anchors = reconciled.anchor_candidate_issues
    skipped = len(issues) - len(anchors)
    if skipped:
        print(f"  Skipped {skipped} gated triage proposal issue(s) (#6778)")
    for issue in anchors:
        if session_exists(f"issue-{issue.number}"):
            print(f"  triage issue #{issue.number}: Already running")
            continue
        # The ADR-0031 §4 marker label declares the anchor's variant; the
        # owner routes it (#6768 B5: queued flavor reaches launch verbatim).
        outcome = queue_recovered_triage_anchor(state, issue)
        if outcome is TriageQueueOutcome.DUPLICATE:
            print(f"  triage issue #{issue.number}: Already queued")
            continue
        print(f"  triage issue #{issue.number}: Queued ({issue.title})")
    if state.pending_triage_reviews:
        print(
            f"  Found {len(state.pending_triage_reviews)} triage review(s)"
            " to process"
        )


def record_health_review_creation(
    action: CreateTriageIssueAction,
    state: "OrchestratorState",
    store: "Optional[QueueCacheStore]",
    now: Optional[float] = None,
) -> None:
    """Record a successful anchor creation (marker-labeled actions only).

    Stamps the in-memory state (stops the next tick re-firing) and persists
    the marker durably (stops a restart re-firing). Persistence failure is
    logged, not raised: the created issue must not be reported as an apply
    failure, the in-memory stamp plus the open anchor's marker label guard
    the current process, and a restart reconciles the durable value from the
    anchor issue itself (:func:`hydrate_last_health_review_at`) — the
    external side effect and the durable timestamp cannot silently diverge.
    """
    if not has_health_review_marker(action.labels):
        return
    stamped_at = time.time() if now is None else now
    state.last_health_review_at = stamped_at
    if store is None:
        return
    try:
        store.save_last_health_review_at(stamped_at)
    except Exception:
        logger.warning(
            "Failed to persist last_health_review_at; a restart will reconcile "
            "the interval from the anchor issue's creation time",
            exc_info=True,
        )


def hydrate_last_health_review_at(
    config: "Config",
    state: "OrchestratorState",
    store: "Optional[QueueCacheStore]",
    repository_host: "RepositoryHost",
) -> None:
    """Hydrate the last-fired marker at startup, reconciling with anchor truth.

    The store is the fast path, but the anchor issues themselves are the
    crash-safe truth (ADR-0013): if persisting the stamp failed after an
    anchor was created (disk full, SQLite error), the store is BEHIND — once
    that anchor closes, plain store hydration would re-fire the review before
    the interval elapses. Reconcile by deriving the last-fired time from the
    newest marker-labeled anchor in scope (open or closed) and taking the
    newer of the two; the reconciled value is persisted back so the store
    self-heals. Costs one GitHub call, at startup, only when the trigger is
    armed.
    """
    stored = store.load_last_health_review_at() if store is not None else 0.0
    state.last_health_review_at = stored
    if health_review_interval_minutes(config) <= 0:
        return
    anchored = most_recent_health_anchor_created_at(repository_host, config)
    if anchored <= stored:
        return
    logger.info(
        "Reconciled last_health_review_at from anchor truth: store=%.2f anchor=%.2f",
        stored,
        anchored,
    )
    state.last_health_review_at = anchored
    if store is None:
        return
    try:
        store.save_last_health_review_at(anchored)
    except Exception:
        logger.warning(
            "Failed to self-heal persisted last_health_review_at; the next "
            "restart will reconcile from the anchor issue again",
            exc_info=True,
        )


def most_recent_health_anchor_created_at(
    repository_host: "RepositoryHost", config: "Config"
) -> float:
    """Created-at epoch of the newest in-scope health anchor (0.0 when none)."""
    issues = repository_host.list_issues(
        labels=_anchor_query_labels(config, HEALTH_REVIEW_MARKER_LABEL),
        state="all",
        limit=_ANCHOR_SCAN_LIMIT,
    )
    scoped = [
        issue
        for issue in _scoped_issues(issues, config.filtering.label)
        if has_health_review_marker(issue.labels)
    ]
    return max((_created_at_epoch(issue) for issue in scoped), default=0.0)


def _created_at_epoch(issue: "Issue") -> float:
    """Parse an issue's ISO-8601 ``created_at`` into an epoch timestamp."""
    raw = issue.created_at
    if raw is None:
        return 0.0
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
