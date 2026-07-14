"""Tech-lead reaction classification tests (#6780)."""

from unittest.mock import Mock

from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.planner_types import OrchestratorSnapshot
from issue_orchestrator.control.triage_reaction import TriageReactionPolicy
from issue_orchestrator.domain.models import (
    DiscoveredFailure,
    Issue,
    PendingTriageReview,
)
from issue_orchestrator.domain.triage_session import TriageSessionFlavor
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.repository_host import DependencyIssueSnapshot


class _IssueChecker:
    def __init__(self, states: dict[int, str]) -> None:
        self._states = states

    def get_dependency_issue_snapshot(
        self, issue_number: int, repo: str | None = None
    ) -> DependencyIssueSnapshot | None:
        del repo
        state = self._states.get(issue_number)
        if state is None:
            return None
        return DependencyIssueSnapshot(state=state, milestone="M1")


def _config(*, threshold: int = 3, window_minutes: int = 5) -> Config:
    config = Config()
    config.triage_review_agent = "agent:triage"
    config.triage_review_on_failure = True
    config.triage.health_review.storm_threshold = threshold
    config.triage.health_review.storm_window_minutes = window_minutes
    return config


def _issue(number: int, *, body: str = "") -> Issue:
    return Issue(
        number=number,
        title=f"Issue {number}",
        body=body,
        labels=["agent:test"],
        state="open",
        milestone="M1",
    )


def _blocked(
    number: int = 42,
    *,
    observed_at: float = 1_000.0,
    label: str = "",
    issue_body: str = "",
) -> DiscoveredFailure:
    return DiscoveredFailure(
        issue_number=number,
        issue_title=f"Issue {number}",
        failure_reason="blocked",
        observed_at=observed_at,
        blocking_label=label,
        issue_body=issue_body,
        issue_milestone="M1",
    )


def _failed(number: int, *, observed_at: float = 1_000.0) -> DiscoveredFailure:
    return DiscoveredFailure(
        issue_number=number,
        issue_title=f"Issue {number}",
        failure_reason="failed",
        observed_at=observed_at,
    )


def _policy(
    config: Config,
    *,
    dependency_states: dict[int, str] | None = None,
    now: float = 1_100.0,
) -> TriageReactionPolicy:
    evaluator = (
        DependencyEvaluator(
            issue_checker=_IssueChecker(dependency_states),
            events=Mock(),
        )
        if dependency_states is not None
        else None
    )
    return TriageReactionPolicy(
        config=config,
        labels=LabelManager(config),
        dependency_evaluator=evaluator,
        clock=lambda: now,
    )


def _snapshot(
    *,
    issues: tuple[Issue, ...] = (),
    problems: tuple[DiscoveredFailure, ...] = (),
    pending: tuple[PendingTriageReview, ...] = (),
) -> OrchestratorSnapshot:
    return OrchestratorSnapshot(
        issues=issues,
        active_sessions=(),
        pending_reviews=(),
        pending_reworks=(),
        discovered_failures=problems,
        pending_triage=pending,
        paused=False,
    )


def test_unexplained_block_with_dependents_triggers_investigation() -> None:
    problem = _blocked()
    reaction = _policy(_config()).assess(
        _snapshot(
            issues=(
                _issue(42),
                _issue(43, body="Depends-on: #42"),
            ),
            problems=(problem,),
        )
    )

    assert reaction.investigations == (problem,)
    assert reaction.storm_problems == ()


def test_dependency_satisfied_but_still_blocked_is_unexplained() -> None:
    problem = _blocked()
    reaction = _policy(_config(), dependency_states={7: "closed"}).assess(
        _snapshot(
            issues=(
                _issue(42, body="Depends-on: #7"),
                _issue(43, body="Depends-on: #42"),
            ),
            problems=(problem,),
        )
    )

    assert reaction.investigations == (problem,)


def test_open_tracked_dependency_explains_plain_block() -> None:
    reaction = _policy(_config(), dependency_states={7: "open"}).assess(
        _snapshot(
            issues=(
                _issue(42, body="Depends-on: #7"),
                _issue(43, body="Depends-on: #42"),
            ),
            problems=(_blocked(),),
        )
    )

    assert reaction.investigations == ()
    assert reaction.storm_problems == ()


def test_embedded_completion_dependency_facts_explain_block_when_issue_filtered() -> None:
    """The completed source issue need not remain in the available-work queue."""
    reaction = _policy(_config(), dependency_states={7: "open"}).assess(
        _snapshot(
            issues=(_issue(43, body="Depends-on: #42"),),
            problems=(_blocked(issue_body="Depends-on: #7"),),
        )
    )

    assert reaction.investigations == ()
    assert reaction.storm_problems == ()


def test_blocked_failed_label_is_unexplained_even_with_open_dependency() -> None:
    config = _config()
    problem = _blocked(label=LabelManager(config).blocked_failed.upper())
    reaction = _policy(config, dependency_states={7: "open"}).assess(
        _snapshot(
            issues=(
                _issue(42, body="Depends-on: #7"),
                _issue(43, body="Depends-on: #42"),
            ),
            problems=(problem,),
        )
    )

    assert reaction.investigations == (problem,)


def test_unexplained_block_without_dependents_does_not_spawn_investigation() -> None:
    reaction = _policy(_config()).assess(
        _snapshot(issues=(_issue(42),), problems=(_blocked(),))
    )

    assert reaction.investigations == ()


def test_problem_storm_replaces_individual_investigations() -> None:
    problems = (_failed(9), _failed(3), _blocked(6))
    reaction = _policy(_config(threshold=3)).assess(
        _snapshot(
            issues=(
                _issue(6),
                _issue(7, body="Depends-on: #6"),
            ),
            problems=problems,
        )
    )

    assert reaction.investigations == ()
    assert tuple(p.issue_number for p in reaction.storm_problems) == (3, 6, 9)


def test_recent_queued_problems_join_new_discovery_across_ticks() -> None:
    queued = tuple(
        PendingTriageReview(
            issue_number=number,
            title=f"Investigate {number}",
            flavor=TriageSessionFlavor.FAILURE_INVESTIGATION,
            failure=_failed(number, observed_at=1_000.0),
        )
        for number in (1, 2)
    )

    reaction = _policy(_config(threshold=3), now=1_100.0).assess(
        _snapshot(problems=(_failed(3, observed_at=1_090.0),), pending=queued)
    )

    assert reaction.investigations == ()
    assert tuple(p.issue_number for p in reaction.storm_problems) == (1, 2, 3)


def test_expired_problem_does_not_count_toward_storm() -> None:
    old = _failed(1, observed_at=700.0)
    recent = (_failed(2), _failed(3))
    reaction = _policy(_config(threshold=3), now=1_100.0).assess(
        _snapshot(problems=(old, *recent))
    )

    assert reaction.storm_problems == ()
    assert reaction.investigations == (old, *recent)


def test_legacy_pending_timestamp_cannot_make_storm_unbounded() -> None:
    legacy = _failed(1, observed_at=0.0)
    pending = PendingTriageReview(
        issue_number=1,
        title="Investigate 1",
        flavor=TriageSessionFlavor.FAILURE_INVESTIGATION,
        failure=legacy,
    )
    reaction = _policy(_config(threshold=3)).assess(
        _snapshot(
            problems=(_failed(2), _failed(3)),
            pending=(pending,),
        )
    )

    assert reaction.storm_problems == ()
    assert tuple(p.issue_number for p in reaction.investigations) == (2, 3)
