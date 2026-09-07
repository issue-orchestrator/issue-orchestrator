"""Dependency pressure influences real scheduling without overriding admission."""

from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.domain.models import Issue
from tests.unit.test_scheduler import CollectingEventSink, MockIssueChecker


def issue(number, body="", *, title="Untitled", state="open"):
    return Issue(number=number, title=title, body=body, labels=[], milestone="M1", state=state)


def scheduler_for(config, issues):
    checker = MockIssueChecker()
    checker.issues = {item.number: item.state for item in issues}
    config.max_concurrent_sessions = 1
    config.repo = "test/repo"
    return Scheduler(config, dependency_evaluator=DependencyEvaluator(
        issue_checker=checker, events=CollectingEventSink(), repo=config.repo,
    ))


def test_transitive_diamond_root_wins_equal_priority_but_only_available_issues_launch(sample_config):
    issues = [issue(1), issue(20), issue(21, "Depends-on: #20"),
              issue(22, "Depends-on: #20"), issue(23, "Depends-on: #21\nDepends-on: #22")]
    scheduler = scheduler_for(sample_config, issues)
    pressure = scheduler.dependency_pressure(issues)
    available, _ = scheduler.get_available_issues(issues)
    assert pressure.count_for(20) == 3
    assert [item.number for item in scheduler.pick_next_batch(available, 0, pressure=pressure)] == [20]
    assert [item.number for item in scheduler.pick_next_batch(available, 0, [1], pressure=pressure)] == [1]


def test_explicit_title_sequence_precedes_dependency_pressure(sample_config):
    issues = [issue(1, title="[P1-001] First"), issue(20, title="[P1-002] Root"),
              issue(21, "Depends-on: #20")]
    scheduler = scheduler_for(sample_config, issues)
    pressure = scheduler.dependency_pressure(issues)
    available, _ = scheduler.get_available_issues(issues)
    assert [item.number for item in scheduler.pick_next_batch(available, 0, pressure=pressure)] == [1]


def test_closed_prose_invalid_and_cross_repository_edges_do_not_inflate_weight(sample_config):
    issues = [issue(20), issue(21, "Depends-on: #20", state="closed"),
              issue(22, "We discussed #20"), issue(23, "Depends-on: other/repo#20"),
              issue(24, "Depends-on: #20\nStack-after: #20")]
    scheduler = scheduler_for(sample_config, issues)
    assert scheduler.dependency_pressure(issues).count_for(20) == 0


def test_explicit_same_repository_reference_counts(sample_config):
    issues = [issue(20), issue(21, "Depends-on: test/repo#20")]
    scheduler = scheduler_for(sample_config, issues)
    assert scheduler.dependency_pressure(issues).count_for(20) == 1


def test_cycles_terminate_without_counting_root_as_its_own_dependent(sample_config):
    issues = [issue(20, "Depends-on: #21"), issue(21, "Depends-on: #20")]
    scheduler = scheduler_for(sample_config, issues)
    pressure = scheduler.dependency_pressure(issues)
    assert pressure.count_for(20) == 0
    assert scheduler.get_available_issues(issues)[0] == []


def test_planner_consumes_pressure_and_preserves_computed_reason(sample_config):
    from issue_orchestrator.control.actions import LaunchSessionAction
    from issue_orchestrator.control.planner import Planner
    from tests.unit.test_planner import make_snapshot

    issues = [issue(1), issue(20), issue(21, "Depends-on: #20")]
    scheduler = scheduler_for(sample_config, issues)
    planner = Planner(config=sample_config, scheduler=scheduler,
                      dependency_evaluator=scheduler.dependency_evaluator)
    plan = planner.plan(make_snapshot(issues=issues))
    launches = [action for action in plan.actions if isinstance(action, LaunchSessionAction)]
    assert [action.number for action in launches] == [20]
    assert "dependency_fanout=1" in launches[0].reason


def test_open_root_outside_scope_is_distinguished_without_guessing_missing_label(sample_config):
    root = issue(20)
    dependent = issue(21, "Stack-after: #20")
    scheduler = scheduler_for(sample_config, [root, dependent])
    decision, = scheduler.evaluate_issues([dependent])
    assert decision.outside_scope_predecessors == (20,)
    assert "predecessor_outside_scheduler_scope: #20" in decision.detail
    assert "no agent" not in decision.detail
    visible = scheduler.evaluate_issues([root, dependent])[1]
    assert visible.outside_scope_predecessors == ()
    assert "predecessor_outside_scheduler_scope" not in visible.detail


def test_unknown_or_closed_predecessor_is_not_reported_as_outside_scope(sample_config):
    dependent = issue(21, "Depends-on: #20")
    scheduler = scheduler_for(sample_config, [dependent])
    decision, = scheduler.evaluate_issues([dependent])
    assert decision.outside_scope_predecessors == ()
    scheduler = scheduler_for(sample_config, [issue(20, state="closed"), dependent])
    decision, = scheduler.evaluate_issues([dependent])
    assert decision.available
    assert decision.outside_scope_predecessors == ()


def test_outside_scope_summary_reaches_health_board_and_clears_when_root_enters_scope(sample_config):
    from datetime import datetime, timezone
    from unittest.mock import Mock

    from issue_orchestrator.control.board_snapshot_builder import BoardSnapshotBuilder
    from issue_orchestrator.control.github_workflow import GitHubWorkflow
    from issue_orchestrator.domain.models import OrchestratorState
    from issue_orchestrator.events import EventContext

    root, dependent = issue(20), issue(21, "Depends-on: #20")
    scheduler = scheduler_for(sample_config, [root, dependent])
    events = CollectingEventSink()
    workflow = GitHubWorkflow(config=sample_config, events=events,
        repository_host=Mock(), fact_gatherer=Mock(), pr_scanner=Mock(),
        label_sync=None, event_context=EventContext())
    builder = BoardSnapshotBuilder(timeline_reader=lambda number, limit: (),
        log_tail_provider=lambda count: [], case_file_reader=lambda: (),
        shipped_fix_reader=lambda limit: (), e2e_health_reader=lambda now: None,
        session_activity_reader=lambda session: None,
        clock=lambda: datetime(2026, 9, 6, tzinfo=timezone.utc))
    state = OrchestratorState()
    workflow.update_dependency_problems(state, scheduler.get_available_issues([dependent])[1])
    assert "predecessor_outside_scheduler_scope: #20" in builder.build(state).blocked_issues[0].summary
    assert "predecessor_outside_scheduler_scope: #20" in events.events[0].data["summary"]
    workflow.update_dependency_problems(state, scheduler.get_available_issues([root, dependent])[1])
    assert "predecessor_outside_scheduler_scope" not in builder.build(state).blocked_issues[0].summary


def test_ready_stack_edge_contributes_no_work_blocking_pressure(sample_config):
    from issue_orchestrator.domain.dependency_gates import PredecessorFacts

    class ReadyStackFacts:
        def gather_facts(self, targets):
            return {target: PredecessorFacts(branch_usable=True, validation_passed=True,
                agent_reviewed=True, branch_name="ready", head_sha="a" * 40)
                for target in targets}

    issues = [issue(20), issue(21, "Stack-after: #20")]
    checker = MockIssueChecker()
    checker.issues = {20: "open", 21: "open"}
    scheduler = Scheduler(sample_config, dependency_evaluator=DependencyEvaluator(
        issue_checker=checker, events=CollectingEventSink(), predecessor_facts_provider=ReadyStackFacts()))
    assert scheduler.dependency_pressure(issues).count_for(20) == 0
    assert scheduler.evaluate_issues(issues)[1].available


def test_outside_scope_diagnostic_is_preserved_in_keyboard_accessible_issue_row(sample_config):
    from pathlib import Path
    from bs4 import BeautifulSoup
    from jinja2 import Environment, FileSystemLoader

    root, dependent = issue(20), issue(21, "Depends-on: #20")
    scheduler = scheduler_for(sample_config, [root, dependent])
    decision, = scheduler.evaluate_issues([dependent])
    templates = Path(__file__).resolve().parents[2] / "src/issue_orchestrator/templates"
    template = Environment(loader=FileSystemLoader(templates), autoescape=True).get_template("issue_row.html")
    html = template.render(issue={"issue_number": 21, "title": dependent.title,
        "status": "blocked", "has_dependencies": True, "dependency_summary": decision.detail,
        "detail_label": "Blocked", "detail_reason": decision.detail,
        "action": "details", "action_hint": "View dependency details"}, active_tab="issues")
    soup = BeautifulSoup(html, "html.parser")
    row = soup.select_one(".issue-row")
    assert row["data-dependency-summary"] == decision.detail
    assert row["tabindex"] == "0"
    assert row["aria-label"]
    assert soup.select_one(".dep-icon")["aria-label"] == f"Has dependencies: {decision.detail}"


def test_deadlocked_chain_gives_no_root_weight_while_acyclic_chain_still_does(sample_config):
    issues = [issue(1), issue(20), issue(21, "Depends-on: #20\nDepends-on: #22"),
              issue(22, "Depends-on: #21"), issue(23, "Depends-on: #20\nDepends-on: #21"),
              issue(30), issue(31, "Depends-on: #30")]
    scheduler = scheduler_for(sample_config, issues)
    pressure = scheduler.dependency_pressure(issues)
    assert pressure.count_for(20) == 0
    assert pressure.count_for(21) == 0
    assert pressure.count_for(30) == 1
    available, _ = scheduler.get_available_issues(issues)
    assert [item.number for item in scheduler.pick_next_batch(available, 0, pressure=pressure)] == [30]


def test_malformed_dependent_cannot_inflate_an_otherwise_valid_roots_weight(sample_config):
    issues = [issue(20), issue(21, "Depends-on: #20\nStack-after: ???")]
    scheduler = scheduler_for(sample_config, issues)
    assert scheduler.dependency_pressure(issues).count_for(20) == 0


def test_cross_milestone_invalid_chain_cannot_outweigh_a_releasable_chain(sample_config):
    from issue_orchestrator.ports.repository_host import DependencyIssueSnapshot

    class MilestoneChecker(MockIssueChecker):
        def get_dependency_issue_snapshot(self, issue_number, repo=None):
            if issue_number == 90:
                return DependencyIssueSnapshot(state="closed", milestone="M2")
            return super().get_dependency_issue_snapshot(issue_number, repo)

    issues = [issue(20), issue(21, "Depends-on: #20\nDepends-on: #90"),
              issue(22, "Depends-on: #21"), issue(30), issue(31, "Depends-on: #30")]
    checker = MilestoneChecker()
    checker.issues = {item.number: item.state for item in issues}
    sample_config.max_concurrent_sessions = 1
    scheduler = Scheduler(sample_config, dependency_evaluator=DependencyEvaluator(
        issue_checker=checker, events=CollectingEventSink(), repo=sample_config.repo))
    pressure = scheduler.dependency_pressure(issues)
    assert pressure.count_for(20) == 0
    assert pressure.count_for(21) == 0
    assert pressure.count_for(30) == 1
    available, _ = scheduler.get_available_issues(issues)
    assert [item.number for item in scheduler.pick_next_batch(available, 0, pressure=pressure)] == [30]
