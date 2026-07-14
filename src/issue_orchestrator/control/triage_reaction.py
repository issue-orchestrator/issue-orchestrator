"""Tech-lead reaction policy for blocked/failed issue problems (#6780).

This is the single policy owner for the reactive side of ADR-0031:

* failed/timed-out sessions remain immediate investigation candidates;
* an explicit block is investigated only when dependency policy cannot
  explain it as healthy waiting on a tracked open issue, and only when the
  blocked issue has downstream dependents;
* a time-bounded cohort at or above the configured storm threshold suppresses
  per-issue investigations and requests one unscheduled health review.

The classifier is pure and deterministic. It consumes the immutable planner
snapshot plus an injected clock and returns facts for the planner to map onto
actions. The completion-side helper records problem facts at the same policy
boundary. Queue mutation and GitHub issue creation stay at their existing
owner boundaries.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from ..domain.dependency_gates import Gate, GateBlockReason
from ..domain.models import DiscoveredFailure, Session, SessionStatus
from ..domain.session_key import TaskKind
from .dependency_gate_snapshot import build_successor_index
from .triage_session_policy import is_triage_session

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports import Issue
    from .dependency_evaluator import DependencyEvaluator
    from .label_manager import LabelManager
    from .planner_types import OrchestratorSnapshot


@dataclass(frozen=True, slots=True)
class TriageReaction:
    """One tick's reaction outcome.

    Exactly one collection is populated: a storm replaces individual
    investigations for every member in its cohort.
    """

    investigations: tuple[DiscoveredFailure, ...] = ()
    storm_problems: tuple[DiscoveredFailure, ...] = ()

    @property
    def storm_issue_numbers(self) -> frozenset[int]:
        return frozenset(problem.issue_number for problem in self.storm_problems)


_REACTIVE_SESSION_STATUSES = frozenset(
    (SessionStatus.FAILED, SessionStatus.TIMED_OUT, SessionStatus.BLOCKED)
)


def record_completed_session_problem(
    *,
    status: SessionStatus,
    session: Session,
    triage_agent: str | None,
    blocking_label: str,
    artifact_hints: Callable[[], tuple[str, ...]],
    record: Callable[[DiscoveredFailure], None],
    clock: Callable[[], float] = time.time,
) -> None:
    """Record one reactive worker-session fact at the state owner boundary.

    Completion coordinates mechanics; this reaction owner decides which
    terminal outcomes are problem facts. Artifact gathering stays lazy so a
    successful or non-worker session performs no filesystem work.
    """
    if status not in _REACTIVE_SESSION_STATUSES:
        return
    if session.key.task is not TaskKind.CODE:
        return
    if is_triage_session(triage_agent, session.issue.agent_type):
        return
    record(
        DiscoveredFailure(
            session.issue.number,
            session.issue.title,
            status.value,
            artifact_hints=artifact_hints(),
            observed_at=clock(),
            blocking_label=blocking_label,
            issue_body=session.issue.body or "",
            issue_milestone=session.issue.milestone,
        )
    )


class TriageReactionPolicy:
    """Classify triage-worthy problems without mutating queues or state."""

    def __init__(
        self,
        *,
        config: "Config",
        labels: "LabelManager",
        dependency_evaluator: "DependencyEvaluator | None",
        clock: Callable[[], float],
    ) -> None:
        self._config = config
        self._labels = labels
        self._dependency_evaluator = dependency_evaluator
        self._clock = clock

    def assess(self, snapshot: "OrchestratorSnapshot") -> TriageReaction:
        """Return the individual or storm reaction for ``snapshot``.

        Pending failure investigations participate in storm detection so
        adjacent ticks can coalesce before launch. They are not returned as
        new investigation requests; the pending-queue owner already holds
        them.
        """
        if (
            not self._config.triage_review_on_failure
            or not self._config.triage_review_agent
        ):
            return TriageReaction()

        now = self._clock()
        pending = {
            item.issue_number: item.failure
            for item in snapshot.pending_triage
            if item.failure is not None
        }
        discovered = {
            problem.issue_number: problem
            for problem in snapshot.discovered_failures
        }
        problems = {**pending, **discovered}
        issue_by_number = {issue.number: issue for issue in snapshot.issues}
        successors = build_successor_index(snapshot.issues)

        storm_candidates: list[DiscoveredFailure] = []
        investigations: list[DiscoveredFailure] = []
        already_queued = {item.issue_number for item in snapshot.pending_triage}

        for problem in problems.values():
            is_block = problem.failure_reason == "blocked"
            if is_block and self._is_explained_block(
                problem, issue_by_number.get(problem.issue_number)
            ):
                continue

            # A zero timestamp is a compatibility value on legacy queue
            # entries. A problem discovered in THIS snapshot is current; an
            # already-pending legacy entry has no trustworthy observation time
            # and therefore cannot count toward a time-bounded storm.
            observed_now = problem.issue_number in discovered
            if self._inside_storm_window(problem, now, observed_now=observed_now):
                storm_candidates.append(problem)

            if problem.issue_number in already_queued:
                continue
            if is_block and not successors.get(problem.issue_number):
                continue
            investigations.append(problem)

        threshold = self._config.triage.health_review.storm_threshold
        if threshold > 0 and len(storm_candidates) >= threshold:
            return TriageReaction(
                storm_problems=tuple(
                    sorted(storm_candidates, key=lambda item: item.issue_number)
                )
            )
        return TriageReaction(
            investigations=tuple(
                sorted(investigations, key=lambda item: item.issue_number)
            )
        )

    def _is_explained_block(
        self, problem: DiscoveredFailure, issue: "Issue | None"
    ) -> bool:
        """True only for a plain block waiting on a tracked open dependency."""
        if (
            problem.blocking_label.casefold()
            == self._labels.blocked_failed.casefold()
        ):
            return False
        if self._dependency_evaluator is None:
            return False
        body = issue.body if issue is not None else problem.issue_body
        if not body:
            return False
        report = self._dependency_evaluator.evaluate_all_gates(
            problem.issue_number,
            body,
            issue.milestone if issue is not None else problem.issue_milestone,
        )
        return GateBlockReason.DEPENDENCY_OPEN in report.reason_codes(Gate.WORK)

    def _inside_storm_window(
        self,
        problem: DiscoveredFailure,
        now: float,
        *,
        observed_now: bool,
    ) -> bool:
        observed_at = problem.observed_at
        if observed_at <= 0:
            return observed_now
        window_seconds = (
            self._config.triage.health_review.storm_window_minutes * 60
        )
        return observed_at <= now and now - observed_at <= window_seconds
