"""Tech-lead attention sweep for terminally-stuck issues (#6823).

Single policy owner for the reactive backstop that the normal control loop
cannot reach: an open issue that carries a terminal blocking label but is NOT
in any active session, pending triage queue, or storm cohort has fallen out of
the loop entirely — no completion will ever re-discover it, so it sits stuck
forever. This module finds those issues on a bounded, timer-gated cadence and
re-injects each one into the EXISTING reactive-triage pipeline as a recovered
:class:`DiscoveredFailure`, so the tech-lead reaction model investigates it and
the triage agent proposes/executes ``reset_retry`` (or closes it if the work is
actually done).

Design boundaries (kept deliberately narrow, ADR-0031):

* **All policy lives here.** The planner, ``fact_gatherer`` and
  ``triage_reaction`` gain no new branchy control vocabulary — the fact
  gatherer only arms this owner and records what it returns.
* **Observation, not mutation.** :func:`run_stuck_sweep` reads GitHub and
  mutates only orchestrator STATE (the durable per-issue recovery counter and
  the injected discovered failures). It never writes a GitHub label directly —
  the recovered failure rides the Observer -> Planner -> Applier chain like any
  other discovered problem.
* **``failure_reason`` is always ``timed_out``.** The reaction model's
  ``_disposition`` only applies the "no downstream dependents -> IGNORE" gate
  when ``failure_reason == "blocked"``; ``timed_out`` always yields
  INVESTIGATE. The recovered failure preserves the issue's REAL terminal label
  in ``blocking_label`` for context, but reports ``timed_out`` so a leaf stuck
  issue is still investigated rather than silently dropped.
* **Bounded / escalating.** Each recovery increments a durable per-issue
  counter (:attr:`OrchestratorState.recovery_attempts`). Once an issue has been
  recovered ``max_recovery_attempts`` times it is NOT re-injected again (no
  infinite loop); it is surfaced as exhausted for the caller to escalate. v1
  logs/emits the exhaustion rather than writing the needs-human label directly
  — wiring a planner escalation action is a follow-up (see :class:`StuckSweepResult`).
* **Dedup / cooldown.** Issues owned by an active session, a pending triage
  review, a pending storm cohort, or this tick's discovered failures are
  skipped — the tech lead is the backstop, never a competitor. The
  pending-triage dedup is also the cooldown for v1: once an issue is recovered
  it becomes a pending failure investigation and is skipped on every subsequent
  sweep until that investigation completes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..domain.models import DiscoveredFailure, SessionStatus
from ..domain.triage_session import PROPOSED_TRIAGE_LABEL, TRIAGE_OBSERVATION_LABEL

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..domain.models import OrchestratorState
    from ..ports import Issue, RepositoryHost
    from ..ports.queue_cache_store import QueueCacheStore
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

# One bounded, timer-gated scan of open blocking-labeled issues. Not
# ``exhaustive``: a partial page just defers a straggler to the next cadence
# (unlike the health-review anchor scan, a missed issue here is re-swept in
# ``interval_minutes``, never a duplicate/lost-anchor correctness bug), and
# failing the whole tick loud on a large open backlog is worse than recovering
# a subset now. Gated by ``stuck_sweep_due`` so a disabled/not-due sweep makes
# ZERO GitHub calls.
STUCK_SWEEP_SCAN_LIMIT = 1000


@dataclass(frozen=True)
class StuckSweepResult:
    """The sweep's outcome for the fact gatherer to consume.

    ``recovered`` are the failures to inject into ``discovered_failures`` (each
    already counted against its durable recovery budget). ``exhausted`` are the
    issue numbers that hit ``max_recovery_attempts`` and were deliberately NOT
    re-injected — the escalation intent. v1 logs/emits them; a follow-up wires a
    planner action that applies the needs-human label through the Applier so the
    escalation is orchestrator-authoritative rather than a direct GitHub write
    from this observation seam.
    """

    recovered: tuple[DiscoveredFailure, ...] = ()
    exhausted: tuple[int, ...] = field(default_factory=tuple)


def stuck_sweep_due(config: "Config", state: "OrchestratorState", now: float) -> bool:
    """True when the timer-gated tech-lead sweep should run this tick.

    Pure state/config math — ZERO GitHub calls — so an disabled or not-yet-due
    sweep never touches the network (mirrors ``health_review_due``). The sweep
    requires a configured triage agent AND triage-on-failure, because the whole
    point is to feed the reactive triage pipeline; without either there is
    nothing to re-inject into.
    """
    sweep = config.triage.stuck_sweep
    if not sweep.enabled:
        return False
    if not (config.triage_review_agent and config.triage_review_on_failure):
        return False
    interval_seconds = sweep.interval_minutes * 60
    return now - state.last_stuck_sweep_at >= interval_seconds


def run_stuck_sweep(
    config: "Config",
    state: "OrchestratorState",
    repository_host: "RepositoryHost",
    label_manager: "LabelManager",
    now: float,
) -> StuckSweepResult:
    """Find stuck issues and return recovered failures + exhausted numbers.

    Mutates ``state.recovery_attempts`` (increments per recovered issue) but
    performs no GitHub writes. The caller records the returned ``recovered``
    failures via ``state.record_discovered_failure`` so the tech-lead reaction
    model investigates each with no new planner code.
    """
    candidates = _scan_stuck_issues(config, repository_host, label_manager)
    owned = _owned_issue_numbers(state)
    max_attempts = config.triage.stuck_sweep.max_recovery_attempts
    recovered: list[DiscoveredFailure] = []
    exhausted: list[int] = []
    for issue, blocking_label in candidates:
        if issue.number in owned:
            continue
        attempts = state.recovery_attempts.get(issue.number, 0)
        if attempts >= max_attempts:
            exhausted.append(issue.number)
            logger.warning(
                "[STUCK_SWEEP] issue #%d exhausted recovery budget "
                "(%d/%d attempts, label=%s); not re-injecting — needs human "
                "attention (#6823)",
                issue.number,
                attempts,
                max_attempts,
                blocking_label,
            )
            continue
        state.recovery_attempts[issue.number] = attempts + 1
        recovered.append(_recovered_failure(issue, blocking_label, now))
        logger.info(
            "[STUCK_SWEEP] re-injecting stuck issue #%d as a recovered failure "
            "(attempt %d/%d, label=%s) (#6823)",
            issue.number,
            attempts + 1,
            max_attempts,
            blocking_label,
        )
    return StuckSweepResult(recovered=tuple(recovered), exhausted=tuple(exhausted))


def hydrate_stuck_sweep_state(
    state: "OrchestratorState",
    store: "QueueCacheStore | None",
) -> None:
    """Restore the durable sweep timer and recovery counters at startup.

    The recovery counter is crash-safe truth: without it a restart would reset
    every issue's budget to 0 and re-inject an already-exhausted issue forever.
    Loaded unconditionally when a store is present (even when the sweep is
    disabled) so the counters survive an enable/disable toggle.
    """
    if store is None:
        return
    state.last_stuck_sweep_at = store.load_last_stuck_sweep_at()
    state.recovery_attempts = store.load_recovery_attempts()


def persist_stuck_sweep_state(
    state: "OrchestratorState",
    store: "QueueCacheStore | None",
) -> None:
    """Persist the sweep timer + recovery counters; degrade on failure.

    A persist failure is logged, not raised: the sweep already mutated
    in-memory state and a restart re-hydrates from the store, so a lost write
    at worst re-sweeps one issue early or under-counts one attempt — never a
    crash on the observation path (mirrors ``record_health_review_creation``).
    """
    if store is None:
        return
    try:
        store.save_last_stuck_sweep_at(state.last_stuck_sweep_at)
        store.save_recovery_attempts(state.recovery_attempts)
    except Exception:
        logger.warning(
            "[STUCK_SWEEP] failed to persist recovery counters; a restart "
            "re-hydrates them from the queue-cache store",
            exc_info=True,
        )


def _scan_stuck_issues(
    config: "Config",
    repository_host: "RepositoryHost",
    label_manager: "LabelManager",
) -> list[tuple["Issue", str]]:
    """Scan open issues and return ``(issue, stuck_label)`` for stuck work.

    ONE bounded scoped query (server-side ``filtering.label`` when configured;
    GitHub label filtering is AND-semantics so blocking labels — an OR set —
    are filtered client-side). Only issues STILL carrying a RECOVERABLE stuck
    label survive: triage machinery (proposed-triage gates, observation case
    files), already-human-owned issues (needs-human), and transient provider
    outages are excluded so the sweep never re-triages a gated proposal, a case
    file, a human escalation, or a circuit-broken issue.
    """
    scope = [value for value in (config.filtering.label,) if value] or None
    issues = repository_host.list_issues(
        labels=scope,
        state="open",
        limit=STUCK_SWEEP_SCAN_LIMIT,
    )
    scoped = _scope_filtered(issues, config.filtering.label)
    skip_folded = _non_recoverable_blocking_folded(label_manager)
    preferred = label_manager.blocked_failed.casefold()
    candidates: list[tuple["Issue", str]] = []
    for issue in scoped:
        if issue.state != "open":
            continue
        blocker = _stuck_blocking_label(issue, label_manager, skip_folded, preferred)
        if blocker is None:
            continue
        candidates.append((issue, blocker))
    return candidates


def _scope_filtered(
    issues: list["Issue"], filter_label: str | None
) -> list["Issue"]:
    """Enforce the ``filtering.label`` scope client-side (casefolded).

    The scope is also part of the server query, but adapters/fakes may not
    filter; eligibility is a policy rule and must hold regardless of transport.
    """
    folded = (filter_label or "").casefold()
    return [
        issue
        for issue in issues
        if not folded or any(name.casefold() == folded for name in issue.labels)
    ]


def _stuck_blocking_label(
    issue: "Issue",
    label_manager: "LabelManager",
    skip_folded: frozenset[str],
    preferred_folded: str,
) -> str | None:
    """The issue's recoverable stuck label, or None when not stuck.

    Prefers the ``blocked-failed`` label (the canonical failed-session signal)
    when present, else the first remaining recoverable blocking label. Returns
    None when the issue carries no blocking label, or only non-recoverable ones
    (machinery / human / transient) — the done guard.
    """
    blockers = [
        name
        for name in label_manager.get_blocking(issue.labels)
        if name.casefold() not in skip_folded
    ]
    if not blockers:
        return None
    for name in blockers:
        if name.casefold() == preferred_folded:
            return name
    return blockers[0]


def _non_recoverable_blocking_folded(
    label_manager: "LabelManager",
) -> frozenset[str]:
    """Casefolded blocking labels the sweep must NOT act on.

    Human escalations own their issue already (and are the sweep's OWN
    escalation sink); provider-unavailable is a transient circuit-broken state
    the resilience manager resumes on its own; proposed-triage / observation
    are triage machinery (gated proposals, evidence case files), never stuck
    work items — re-injecting one would launch a triage session on triage's own
    bookkeeping.
    """
    return frozenset(
        {
            label_manager.needs_human.casefold(),
            label_manager.triage_needs_human.casefold(),
            label_manager.provider_unavailable.casefold(),
            PROPOSED_TRIAGE_LABEL.casefold(),
            TRIAGE_OBSERVATION_LABEL.casefold(),
        }
    )


def _owned_issue_numbers(state: "OrchestratorState") -> set[int]:
    """Issue numbers the tech lead must NOT compete for (dedup + cooldown).

    An issue already worked or queued — an active session, a pending triage
    review, a pending storm cohort member, or discovered this very tick — is
    covered by the normal loop; re-injecting it would double-queue. The
    pending-triage membership is also the v1 cooldown: a recovered issue stays
    a pending failure investigation until it completes, so it is skipped on
    every intervening sweep.
    """
    owned = {session.issue.number for session in state.active_sessions}
    owned.update(item.issue_number for item in state.pending_triage_reviews)
    for item in state.pending_triage_reviews:
        owned.update(problem.issue_number for problem in item.problem_cohort)
    owned.update(failure.issue_number for failure in state.discovered_failures)
    return owned


def _recovered_failure(
    issue: "Issue", blocking_label: str, now: float
) -> DiscoveredFailure:
    """Build the recovered failure fact re-injected into reactive triage.

    ``failure_reason`` is ``timed_out`` (never ``blocked``) so the reaction
    model always INVESTIGATES: its no-dependents IGNORE gate only fires for a
    ``blocked`` reason, and a leaf stuck issue must still be investigated. The
    real terminal label rides along in ``blocking_label`` for context.
    """
    return DiscoveredFailure(
        issue_number=issue.number,
        issue_title=issue.title,
        failure_reason=SessionStatus.TIMED_OUT.value,
        blocking_label=blocking_label,
        issue_body=issue.body or "",
        issue_milestone=issue.milestone,
        observed_at=now,
        artifact_hints=(),
    )
