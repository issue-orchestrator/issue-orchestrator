"""Composition wiring for the triage ``reset_retry`` executor (#6764).

Production boundary: the executor's ``run_reset`` reuses
``reset_and_retry_issue`` — the exact per-issue pipeline behind the
dashboard's ``/api/reset-retry`` endpoint — with ``from_scratch=True``
(ADR-0031's action vocabulary defines ``reset_retry`` as reset-and-retry
FROM SCRATCH). Nothing about the reset boundary (runtime termination, PR
superseding, branch deletion, label/history/timeline clearing,
pending-label relaunch marking, queue re-insertion) is reimplemented here.

Lives outside ``bootstrap`` so the composition root stays wiring-only; the
closures read live orchestrator state at EXECUTION time, which is why this
executor can only be wired after the orchestrator is constructed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from ..control.queue_cache import QueueCache
from ..control.triage_reset_retry import (
    ResetRetryRunOutcome,
    TriageResetRetryExecutor,
)

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator
    from ..ports.issue import Issue

# Event provenance for ISSUE_UNBLOCKED emitted by an agent-authorized reset,
# distinguishing it from the operator-clicked "web.reset-retry" source.
TRIAGE_RESET_RETRY_EVENT_SOURCE = "triage.reset_retry"


def build_triage_reset_retry_executor(
    orchestrator: "Orchestrator",
) -> TriageResetRetryExecutor:
    """Build the production executor over the live orchestrator."""
    # Lazy: web_retry_history_routes pulls in the FastAPI routing stack,
    # which composition should not load at module-import time.
    from ..control.maintenance import reset_issue
    from .web_retry_history_routes import (
        has_active_reset_retry_runtime,
        reset_and_retry_issue,
    )

    deps = orchestrator.deps
    label_manager = deps.label_manager

    def _run_reset(
        issue_number: int, current_labels: Sequence[str]
    ) -> ResetRetryRunOutcome:
        queue_cache = QueueCache(
            orchestrator.config, orchestrator.state, deps.queue_cache_store
        )
        success_payload, failure_payload = reset_and_retry_issue(
            issue_number=issue_number,
            from_scratch=True,
            pending_label=label_manager.reset_retry_pending,
            scratch_pending_label=label_manager.reset_retry_scratch_pending,
            repository_host=orchestrator.repository_host,
            queue_cache=queue_cache,
            state=orchestrator.state,
            deps=deps,
            config=orchestrator.config,
            reset_issue_fn=reset_issue,
            current_labels=list(current_labels),
            source=TRIAGE_RESET_RETRY_EVENT_SOURCE,
        )
        if success_payload is not None:
            return ResetRetryRunOutcome(success=True, details=success_payload)
        error = (failure_payload or {}).get("error") or "unknown reset failure"
        return ResetRetryRunOutcome(
            success=False, error=str(error), details=failure_payload or {}
        )

    def _read_issue(issue_number: int) -> "Issue | None":
        return deps.repository_host.get_issue(issue_number)

    def _has_active_issue_runtime(issue_number: int) -> bool:
        # Consults the SAME runtime owners the reset boundary would terminate
        # (via ``_reset_retry_runtime_owners``): visible issue/rework sessions,
        # the persistent coder/reviewer pair, supervised review-exchange jobs,
        # and pending publish retry. A stale proposal thus downgrades before it
        # can tear down hidden live work it never observed (#6777).
        return has_active_reset_retry_runtime(
            issue_number=issue_number,
            state=orchestrator.state,
            deps=deps,
        )

    return TriageResetRetryExecutor(
        events=deps.events,
        label_manager=label_manager,
        read_issue=_read_issue,
        has_active_issue_runtime=_has_active_issue_runtime,
        run_reset=_run_reset,
    )
