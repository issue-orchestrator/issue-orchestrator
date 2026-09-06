"""Tech-lead evidence is discoverable without looking like blocked coding work."""

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from bs4 import BeautifulSoup

from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.control.tech_lead_case_files import split_tech_lead_case_file_issues
from issue_orchestrator.domain.models import Issue, OrchestratorState, SessionHistoryEntry
from issue_orchestrator.infra.config import Config
from issue_orchestrator.entrypoints.web_templates import get_templates
from issue_orchestrator.ports.provider_resilience import NO_PROVIDER_CIRCUIT_STATUS
from issue_orchestrator.ports.tech_lead_run_record_store import NO_TECH_LEAD_RUN_HISTORY
from issue_orchestrator.view_models.dashboard import build_dashboard_view_model
from issue_orchestrator.view_models.tech_lead_board import build_tech_lead_board_view, render_tech_lead_board_md
from issue_orchestrator.view_models.work_queue_projection import project_work_queue


@dataclass
class OrchestratorView:
    state: OrchestratorState
    config: Config
    shutdown_requested: bool = False


def case_file(number=49):
    return Issue(number=number, title="Pattern case file: recurring-timeout",
                 labels=["agent:tech-lead", "TECH-LEAD-OBSERVATION"])


@pytest.mark.parametrize("scope_only", [False, True])
@pytest.mark.parametrize("tab", ["kanban", "blocked"])
def test_case_files_are_absent_from_work_columns_and_counts_but_remain_evidence(scope_only, tab):
    config = Config(repo="porchpin/porchpin")
    config.e2e.enabled = False
    evidence = case_file()
    # The title is deliberately similar: titles must never hide real work.
    work = Issue(number=50, title="Pattern rendering needs a fix", labels=["blocked", "agent:backend"])
    state = OrchestratorState(startup_status="complete", cached_scope_issues=[evidence, work],
                             cached_queue_issues=[work] if scope_only else [evidence, work])
    model = build_dashboard_view_model(OrchestratorView(state, config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS, tech_lead_history=NO_TECH_LEAD_RUN_HISTORY,
        active_tab=tab, e2e_status_provider=lambda _: {"enabled": False, "running": False})
    assert [item["issue_number"] for item in model.blocked_items] == [50]
    assert model.blocked_count == 1
    assert model.queue_total == 1
    assert model.scope_summary["in_scope_total"] == 1
    assert all(item["issue_number"] != 49 for item in model.queue_items)
    assert all(item["issue_number"] != 49 for column in model.flow_columns for item in column["items"])
    html = get_templates().get_template("dashboard.html").render(**model.template_context())
    rendered = BeautifulSoup(html, "html.parser")
    assert rendered.select('[data-issue="49"]') == []
    if tab == "kanban":
        work_card = rendered.select_one('.issue-card[data-issue="50"]')
        assert work_card is not None
        focus = work_card.select_one("button.card-focus")
        assert focus is not None
        assert "Pattern rendering needs a fix" in focus.get_text()
        assert focus.get("tabindex") != "-1"
    assert state.cached_scope_issues == [evidence, work]
    assert Scheduler(config).evaluate_issues([evidence])[0].available is False
    _, case_files = split_tech_lead_case_file_issues(state.cached_scope_issues)
    board = build_tech_lead_board_view(ops=(), gated_proposals=(), case_files=case_files,
        area_counts=(), last_health_review_at=0, now=datetime(2026, 9, 6, tzinfo=timezone.utc))
    assert "recurring-timeout" in render_tech_lead_board_md(board)


def test_old_history_and_retry_cards_do_not_reintroduce_current_case_file():
    evidence = case_file()
    cards = [{"issue_number": 49, "status": "failed"}, {"issue_number": 50, "status": "failed"}]
    projection = project_work_queue(queue_issues=(), scope_issues=[evidence])
    assert projection.work_items(cards, issue_number=lambda card: card["issue_number"]) == [cards[1]]
    assert cards[0]["issue_number"] == 49


def test_startup_does_not_advertise_restored_queue_before_it_is_ready():
    config = Config(repo="porchpin/porchpin")
    config.e2e.enabled = False
    state = OrchestratorState(cached_queue_issues=[case_file(), Issue(number=50, title="Work", labels=[])])
    model = build_dashboard_view_model(OrchestratorView(state, config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS, tech_lead_history=NO_TECH_LEAD_RUN_HISTORY,
        e2e_status_provider=lambda _: {"enabled": False, "running": False})
    assert model.queue_total == 0
    assert model.blocked_count == 0


@pytest.mark.parametrize("status", ["closed", "merged", "completed"])
@pytest.mark.parametrize("tab", ["kanban", "completed", "awaiting-merge"])
def test_history_remains_inspectable_without_reintroducing_evidence_as_work(status, tab):
    config = Config(repo="porchpin/porchpin")
    evidence = case_file()
    work = Issue(number=50, title="Pattern rendering needs a fix", labels=["agent:backend"])
    history = [SessionHistoryEntry(issue_number=item.number, title=item.title,
        agent_type="agent:tech-lead" if item.number == 49 else "agent:backend",
        status=status, completed_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
        runtime_minutes=1, pr_url=f"https://github.com/porchpin/porchpin/pull/{item.number + 100}")
        for item in (evidence, work)]
    state = OrchestratorState(startup_status="complete", cached_scope_issues=[evidence, work],
                             cached_queue_issues=[evidence], session_history=history)
    model = build_dashboard_view_model(OrchestratorView(state, config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS, tech_lead_history=NO_TECH_LEAD_RUN_HISTORY,
        active_tab=tab, e2e_status_provider=lambda _: {"enabled": False, "running": False})
    expected_lane = model.awaiting_merge_items if status == "completed" else model.completed_items
    assert [item["issue_number"] for item in expected_lane] == [50]
    assert model.scope_summary["in_scope_total"] == 1
    assert model.queue_total == 0
    assert all(item["issue_number"] != 49 for column in model.flow_columns for item in column["items"])
    assert any(item["issue_number"] == 49 for item in model.history_items)
    assert state.session_history == history
    rendered = BeautifulSoup(get_templates().get_template("dashboard.html").render(
        **model.template_context()), "html.parser")
    assert rendered.select('[data-issue="49"]') == []


@pytest.mark.parametrize("transition", ["closed", "out_of_scope_failed", "out_of_scope_completed", "removed"])
@pytest.mark.parametrize("restart", [False, True])
def test_refresh_retains_case_file_identity_for_historical_work_lanes(tmp_path, transition, restart):
    from copy import deepcopy
    from dataclasses import replace

    from issue_orchestrator.control.queue_cache import QueueCache
    from issue_orchestrator.control.session_history import SessionHistoryOwner
    from issue_orchestrator.execution.queue_cache_store import QueueCacheStore

    config = Config(repo="porchpin/porchpin")
    evidence = case_file()
    work = Issue(number=50, title="Pattern rendering needs a fix", labels=["agent:tech-lead"])
    status = "completed" if transition == "out_of_scope_completed" else "failed"
    history = [SessionHistoryEntry(issue_number=item.number, title=item.title,
        agent_type="agent:tech-lead", status=status, runtime_minutes=1,
        completed_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
        pr_url=f"https://github.com/porchpin/porchpin/pull/{item.number + 100}")
        for item in (evidence, work)]
    original_labels = [entry.issue_labels for entry in history]
    state = OrchestratorState(startup_status="complete", session_history=history)
    store = QueueCacheStore(tmp_path / "queue.sqlite")
    cache = QueueCache(config, state, store)
    cache.replace_from_refresh([evidence, work])
    if transition == "closed":
        cache.upsert_refreshed_issue(replace(evidence, state="closed"))
        SessionHistoryOwner(state.session_history).reconcile_closed_issue(
            issue_number=49, status_reason="Issue closed; history reconciled",
        )
    elif transition == "removed":
        cache.remove_issue(49)
    else:
        cache.replace_from_refresh([work])
    cache.save_snapshot()
    assert all(issue.number != 49 for issue in state.cached_scope_issues)
    assert all(issue.number != 49 for issue in state.cached_queue_issues)
    assert [entry.issue_labels for entry in history] == original_labels
    if restart:
        reopened = QueueCacheStore(tmp_path / "queue.sqlite")
        state = OrchestratorState(startup_status="complete", session_history=deepcopy(history))
        cache = QueueCache(config, state, reopened)
        cache.restore_work_classifications()
        cache.replace_from_refresh(list(reopened.load_issues(config.repo)))
    model = build_dashboard_view_model(OrchestratorView(state, config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS, tech_lead_history=NO_TECH_LEAD_RUN_HISTORY,
        active_tab="kanban", e2e_status_provider=lambda _: {"enabled": False, "running": False})
    assert model.scope_summary["in_scope_total"] == 1
    assert model.queue_total == 0
    all_work = [item for column in model.flow_columns for item in column["items"]]
    assert {item["issue_number"] for item in all_work} == {50}
    assert any(entry.issue_number == 49 for entry in state.session_history)
    if transition in ("closed", "out_of_scope_completed"):
        assert any(item["issue_number"] == 49 for item in model.history_items)
    rendered = BeautifulSoup(get_templates().get_template("dashboard.html").render(**model.template_context()), "html.parser")
    assert rendered.select('[data-issue="49"]') == []
    assert rendered.select('.issue-card[data-issue="50"]')


def test_history_labels_supply_identity_without_current_cache():
    history = [SessionHistoryEntry(issue_number=49, title="Opaque title", agent_type="agent:backend",
        status="failed", runtime_minutes=1, issue_labels=("tech-lead-observation",))]
    projection = project_work_queue(queue_issues=(), scope_issues=(), history=history)
    assert projection.evidence_numbers == {49}
    assert history[0].issue_labels == ("tech-lead-observation",)


def test_explicit_marker_removal_overrides_retained_and_historical_evidence(tmp_path):
    from dataclasses import replace
    from issue_orchestrator.control.queue_cache import QueueCache
    from issue_orchestrator.execution.queue_cache_store import QueueCacheStore

    config = Config(repo="porchpin/porchpin")
    evidence = case_file()
    history = [SessionHistoryEntry(issue_number=49, title=evidence.title, agent_type="agent:tech-lead",
        status="failed", runtime_minutes=1, issue_labels=tuple(evidence.labels))]
    state = OrchestratorState(session_history=history)
    store = QueueCacheStore(tmp_path / "queue.sqlite")
    cache = QueueCache(config, state, store)
    cache.upsert_refreshed_issue(evidence)
    cache.upsert_refreshed_issue(replace(evidence, labels=["agent:backend"]))
    cache.remove_issue(49)
    restored = OrchestratorState(session_history=history)
    QueueCache(config, restored, QueueCacheStore(tmp_path / "queue.sqlite")).restore_work_classifications()
    projection = project_work_queue(queue_issues=(), scope_issues=(), history=history,
                                   retained_classifications=restored.issue_work_classifications)
    assert projection.evidence_numbers == frozenset()
    assert "TECH-LEAD-OBSERVATION" in history[0].issue_labels
    assert store.load_work_classifications("different/repository") == {}


def test_warm_delta_retains_closed_identity_and_stale_cache_cannot_reclassify_it(tmp_path):
    from dataclasses import replace
    from issue_orchestrator.control.queue_cache import QueueCache
    from issue_orchestrator.execution.queue_cache_store import QueueCacheStore

    config = Config(repo="porchpin/porchpin")
    store = QueueCacheStore(tmp_path / "queue.sqlite")
    state = OrchestratorState()
    cache = QueueCache(config, state, store)
    old = replace(case_file(), labels=["agent:backend"])
    cache.replace_from_refresh([old])
    cache.save_snapshot()
    # A closure delta supplies the marker for the first time, then a crash
    # happens before the older queue snapshot can be replaced.
    cache.replace_from_delta([old], [replace(case_file(), state="closed")])
    assert state.cached_scope_issues == []
    restored = OrchestratorState()
    restarted = QueueCache(config, restored, QueueCacheStore(tmp_path / "queue.sqlite"))
    cached, _ = restarted.restore_snapshot()
    restarted.replace_from_cache(cached)  # GitHub transiently unavailable.
    projection = project_work_queue(queue_issues=restored.cached_queue_issues,
        scope_issues=restored.cached_scope_issues, retained_classifications=restored.issue_work_classifications)
    assert projection.evidence_numbers == {49}
    restarted.replace_from_delta(cached, [])  # No fresh fact contradicts the marker.
    assert project_work_queue(queue_issues=restored.cached_queue_issues,
        scope_issues=restored.cached_scope_issues,
        retained_classifications=restored.issue_work_classifications).evidence_numbers == {49}
    restarted.replace_from_delta(cached, [old])  # Explicit fresh marker removal.
    assert project_work_queue(queue_issues=restored.cached_queue_issues,
        scope_issues=restored.cached_scope_issues,
        retained_classifications=restored.issue_work_classifications).evidence_numbers == frozenset()


@pytest.mark.parametrize("transition", ["stale_cache", "closed", "out_of_scope", "marker_removed"])
def test_runtime_incremental_observations_own_evidence_identity(
    tmp_path, mock_event_sink, mock_repository_host, monkeypatch, transition,
):
    from dataclasses import replace
    from unittest.mock import Mock

    from issue_orchestrator.control.issue_fetch_resilience import IssueFetchResilience
    from issue_orchestrator.control.orchestrator_support import _fetch_and_update_queue
    from issue_orchestrator.control.queue_cache import QueueCache
    from issue_orchestrator.domain.issue_work_classification import IssueWorkClassification
    from issue_orchestrator.execution.queue_cache_store import QueueCacheStore

    monkeypatch.setattr("time.time", lambda: 1_788_700_000.0)
    config = Config(repo="porchpin/porchpin")
    config.fetch_layer_enabled = True
    config.fetch_layer_full_scan_interval_seconds = 3600
    config.fetch_layer_discovery_limit = 10
    config.filtering.label = "tracked"
    evidence = replace(case_file(), labels=["tracked", "agent:tech-lead", "tech-lead-observation"])
    work = Issue(number=50, title="Real work", labels=["tracked", "agent:backend"])
    history = [
        SessionHistoryEntry(issue_number=item.number, title=item.title, agent_type="agent:backend",
                            status="failed", runtime_minutes=1)
        for item in (evidence, work)
    ]
    state = OrchestratorState(startup_status="complete")
    store = QueueCacheStore(tmp_path / "queue.sqlite")
    cache = QueueCache(config, state, store)
    # Simulate an older unmarked queue snapshot surviving a newer identity write.
    stale = replace(evidence, labels=["tracked", "agent:tech-lead"])
    cache.replace_from_refresh([evidence if transition in {"stale_cache", "marker_removed"} else stale, work])
    cache.replace_from_cache([stale, work])
    state.session_history.extend(history)
    state.queue_last_full_scan_at = 1_788_700_000.0
    state.queue_delta_watermark = "2026-09-06T00:00:00Z"
    changes = {
        "stale_cache": [],
        "closed": [replace(evidence, state="closed")],
        "out_of_scope": [replace(evidence, labels=["tech-lead-observation"])],
        "marker_removed": [stale],
    }[transition]
    workflow = Mock()
    workflow.refresh_issues.return_value = []
    workflow.fetch_delta_issues.return_value = (changes, "2026-09-06T01:00:00Z")
    workflow.issue_in_scope.side_effect = lambda issue: "tracked" in issue.labels
    scheduler = Mock()
    scheduler.evaluate_issues.return_value = []
    _fetch_and_update_queue(
        config=config, events=mock_event_sink, state=state,
        repository_host=mock_repository_host, scheduler=scheduler, github_workflow=workflow,
        refresh_requested=False, inflight_stable_ids={},
        issue_fetch_resilience=IssueFetchResilience(config.repo), queue_cache_store=store,
    )
    assert state.queue_last_refresh_mode == "incremental"
    workflow.fetch_all_issues.assert_not_called()
    expected = IssueWorkClassification.WORK if transition == "marker_removed" else IssueWorkClassification.EVIDENCE
    assert state.issue_work_classifications[49] == expected
    reopened_state = OrchestratorState(startup_status="complete", session_history=state.session_history)
    QueueCache(config, reopened_state, QueueCacheStore(tmp_path / "queue.sqlite")).restore_snapshot()
    assert reopened_state.issue_work_classifications[49] == expected
    for current in (state, reopened_state):
        model = build_dashboard_view_model(OrchestratorView(current, config),
            provider_circuit=NO_PROVIDER_CIRCUIT_STATUS, tech_lead_history=NO_TECH_LEAD_RUN_HISTORY,
            active_tab="kanban", e2e_status_provider=lambda _: {"enabled": False, "running": False})
        work_numbers = {item["issue_number"] for column in model.flow_columns for item in column["items"]}
        assert work_numbers == ({49, 50} if transition == "marker_removed" else {50})
        assert any(item.issue_number == 49 for item in current.session_history)
        rendered = BeautifulSoup(get_templates().get_template("dashboard.html").render(
            **model.template_context()), "html.parser")
        assert bool(rendered.select('[data-issue="49"]')) == (transition == "marker_removed")


@pytest.mark.parametrize("discovery", [False, True])
@pytest.mark.parametrize("remove_marker", [False, True])
def test_real_fetch_preserves_excluded_label_changes(
    tmp_path, mock_event_sink, mock_repository_host, monkeypatch, discovery, remove_marker,
):
    from dataclasses import replace
    from unittest.mock import Mock

    from issue_orchestrator.control.fact_gatherer import FactGatherer
    from issue_orchestrator.control.github_workflow import GitHubWorkflow
    from issue_orchestrator.control.issue_fetch_resilience import IssueFetchResilience
    from issue_orchestrator.control.orchestrator_support import _fetch_and_update_queue
    from issue_orchestrator.control.queue_cache import QueueCache
    from issue_orchestrator.domain.issue_work_classification import IssueWorkClassification
    from issue_orchestrator.domain.models import AgentConfig
    from issue_orchestrator.events import EventContext
    from issue_orchestrator.execution.queue_cache_store import QueueCacheStore

    monkeypatch.setattr("time.time", lambda: 1_788_700_000.0)
    config = Config(repo="porchpin/porchpin")
    config.agents = {"agent:tech-lead": AgentConfig(prompt_path=tmp_path / "prompt.md", model="test", timeout_minutes=45)}
    config.filtering.exclude_labels = ["excluded"]
    config.fetch_layer_enabled = True
    config.fetch_layer_full_scan_interval_seconds = 3600
    config.fetch_layer_discovery_limit = 10
    config.fetch_layer_max_hot_issues_per_cycle = 0
    marked = case_file()
    unmarked = replace(marked, labels=["agent:tech-lead"])
    state = OrchestratorState(startup_status="complete")
    store = QueueCacheStore(tmp_path / "queue.sqlite")
    cache = QueueCache(config, state, store)
    cache.replace_from_refresh([marked if remove_marker else unmarked])
    # The observation must survive even when there is no queue candidate to retain.
    cache.remove_issue(49)
    work = Issue(number=50, title="Other work", labels=["agent:tech-lead"])
    cache.upsert_refreshed_issue(work)
    state.session_history.append(SessionHistoryEntry(
        issue_number=49, title=marked.title, agent_type="agent:tech-lead",
        status="failed", runtime_minutes=1,
    ))
    state.queue_last_full_scan_at = 1_788_700_000.0 if discovery else 0
    fresh = unmarked if remove_marker else marked
    fresh = replace(fresh, labels=[*fresh.labels, "excluded"])
    mock_repository_host.issues = [fresh, work]
    scanner = Mock()
    scanner.load_issue_branches.return_value = {}
    scanner.scan_for_reviews.return_value = []
    scanner.scan_for_reworks.return_value = ([], [])
    workflow = GitHubWorkflow(
        config=config, events=mock_event_sink, repository_host=mock_repository_host,
        fact_gatherer=FactGatherer(config, mock_repository_host), pr_scanner=scanner,
        label_sync=None, event_context=EventContext(),
    )
    scheduler = Mock()
    scheduler.evaluate_issues.return_value = []
    _fetch_and_update_queue(
        config=config, events=mock_event_sink, state=state,
        repository_host=mock_repository_host, scheduler=scheduler, github_workflow=workflow,
        refresh_requested=False, inflight_stable_ids={},
        issue_fetch_resilience=IssueFetchResilience(config.repo), queue_cache_store=store,
    )
    assert len(mock_repository_host.list_issues_calls) == 1
    assert state.queue_last_refresh_mode == ("incremental" if discovery else "full")
    assert state.cached_scope_issues == [work]
    assert state.cached_queue_issues == [work]
    expected = IssueWorkClassification.WORK if remove_marker else IssueWorkClassification.EVIDENCE
    reopened = OrchestratorState(startup_status="complete", session_history=state.session_history)
    QueueCache(config, reopened, QueueCacheStore(tmp_path / "queue.sqlite")).restore_snapshot()
    for current in (state, reopened):
        assert current.issue_work_classifications[49] == expected
        model = build_dashboard_view_model(OrchestratorView(current, config),
            provider_circuit=NO_PROVIDER_CIRCUIT_STATUS, tech_lead_history=NO_TECH_LEAD_RUN_HISTORY,
            active_tab="kanban", e2e_status_provider=lambda _: {"enabled": False, "running": False})
        assert model.blocked_count == int(remove_marker)
        rendered = BeautifulSoup(get_templates().get_template("dashboard.html").render(
            **model.template_context()), "html.parser")
        assert bool(rendered.select('[data-issue="49"]')) == remove_marker


@pytest.mark.parametrize("restored_evidence", [False, True])
def test_live_session_work_lane_follows_fresh_marker_without_erasing_session(tmp_path, restored_evidence):
    from dataclasses import replace
    import json
    from pathlib import Path
    import subprocess

    from issue_orchestrator.control.queue_cache import QueueCache
    from issue_orchestrator.domain.issue_key import FakeIssueKey
    from issue_orchestrator.domain.models import AgentConfig, Session
    from issue_orchestrator.domain.session_key import SessionKey, TaskKind
    from tests.unit.session_run_helpers import make_session_run_assets

    config = Config(repo="porchpin/porchpin")
    agent = AgentConfig(prompt_path=tmp_path / "prompt.md", model="test", timeout_minutes=45)
    config.agents = {"agent:tech-lead": agent}
    unmarked = replace(case_file(), labels=["agent:tech-lead"])
    restored_issue = case_file() if restored_evidence else unmarked
    session = Session(
        key=SessionKey(issue=FakeIssueKey("49"), task=TaskKind.CODE), issue=restored_issue,
        agent_config=agent, terminal_id="issue-49", worktree_path=tmp_path,
        branch_name="issue-49", run_assets=make_session_run_assets(tmp_path, session_name="issue-49"),
        started_at=datetime(2026, 9, 6),
    )
    state = OrchestratorState(startup_status="complete", active_sessions=[session])
    cache = QueueCache(config, state)
    for fresh, expected_count in ((None, int(not restored_evidence)), (unmarked, 1), (case_file(), 0), (unmarked, 1)):
        if fresh is not None:
            cache.upsert_refreshed_issue(fresh)
        model = build_dashboard_view_model(OrchestratorView(state, config),
            provider_circuit=NO_PROVIDER_CIRCUIT_STATUS, tech_lead_history=NO_TECH_LEAD_RUN_HISTORY,
            active_tab="kanban", e2e_status_provider=lambda _: {"enabled": False, "running": False})
        assert model.active_count == expected_count
        assert model.active_session_count == 1
        running = next(column for column in model.flow_columns if column["id"] == "running")
        assert running["count"] == expected_count
        assert len(running["items"]) == expected_count
        assert len(model.active_items) == expected_count
        rendered = BeautifulSoup(get_templates().get_template("dashboard.html").render(
            **model.template_context()), "html.parser")
        assert bool(rendered.select('[data-issue="49"]')) == bool(expected_count)
        # Exercise the browser's expanded-list selector and renderer with the
        # actual serialized producer payload, including marker removal.
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const expanded = require('./src/issue_orchestrator/static/js/expanded_column_state.js');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
const context = {
    escapeHtml: escape, escapeAttr: escape, cssEscape: String, document: {},
    compactCardState: {computeCompactCardFingerprint: () => 'fingerprint'},
    formatDashboardTimestamps: () => {},
    localStorage: {getItem: () => null, setItem: () => {}},
    window: {dashboardData: {queueRefreshSeconds: 0}, location: {href: 'http://example.test/'}}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('./src/issue_orchestrator/static/js/dashboard/kanban_columns.js', 'utf8'), context);
process.stdout.write(expanded.getExpandedItemsFromViewModel(payload, 'running')
    .map(item => context.renderExpandedCardHtml(item, 'running', false)).join(''));
"""
        expanded = subprocess.run(
            ["node", "-e", script], input=json.dumps(model.to_dict(), default=str),
            text=True, capture_output=True, check=True, cwd=Path(__file__).resolve().parents[2],
        )
        expanded_dom = BeautifulSoup(expanded.stdout, "html.parser")
        assert bool(expanded_dom.select('[data-issue="49"]')) == bool(expected_count)
        assert state.active_sessions == [session]
        assert session.issue is restored_issue


@pytest.mark.parametrize("mode", ["warm_delta", "degraded"])
@pytest.mark.parametrize("retained", [None, "work", "evidence"])
def test_upgrade_snapshot_identity_survives_scope_filter_and_reopen(tmp_path, mode, retained):
    from dataclasses import replace

    from issue_orchestrator.control.queue_cache import QueueCache
    from issue_orchestrator.domain.issue_work_classification import IssueWorkClassification
    from issue_orchestrator.execution.queue_cache_store import QueueCacheStore

    config = Config(repo="porchpin/porchpin")
    config.filtering.label = "tracked"
    marked = case_file()
    unmarked = replace(marked, labels=["agent:tech-lead"])
    work = Issue(number=50, title="Real work", labels=["tracked", "agent:backend"])
    path = tmp_path / "queue.sqlite"
    store = QueueCacheStore(path)
    # A pre-upgrade snapshot has no identity rows. A retained identity, when
    # present, is newer than this snapshot and deliberately contradicts it.
    store.save_snapshot([unmarked if retained == "evidence" else marked, work],
                        "2026-09-06T00:00:00Z", repo=config.repo)
    if retained is not None:
        store.record_work_classifications(config.repo, {49: IssueWorkClassification(retained)})
    history = [SessionHistoryEntry(issue_number=49, title=marked.title,
                                  agent_type="agent:tech-lead", status="failed", runtime_minutes=1)]
    state = OrchestratorState(startup_status="complete", session_history=history)
    cache = QueueCache(config, state, QueueCacheStore(path))
    cached, watermark = cache.restore_snapshot()
    assert watermark == "2026-09-06T00:00:00Z"
    if mode == "warm_delta":
        cache.replace_from_delta(cached, [])
    else:
        cache.replace_from_cache(cached)
    cache.save_snapshot()
    assert [issue.number for issue in state.cached_scope_issues] == [50]
    expected = IssueWorkClassification.WORK if retained == "work" else IssueWorkClassification.EVIDENCE
    # The filtered snapshot no longer contains #49; its identity survives reopen.
    for fresh in (None, unmarked, marked):
        if fresh is not None:
            cache.replace_from_delta(state.cached_scope_issues, [fresh])
            cache.save_snapshot()
            expected = IssueWorkClassification.WORK if fresh is unmarked else IssueWorkClassification.EVIDENCE
        reopened = OrchestratorState(startup_status="complete", session_history=history)
        restarted = QueueCache(config, reopened, QueueCacheStore(path))
        restored, _ = restarted.restore_snapshot()
        restarted.replace_from_cache(restored)
        assert reopened.issue_work_classifications[49] == expected
        model = build_dashboard_view_model(OrchestratorView(reopened, config),
            provider_circuit=NO_PROVIDER_CIRCUIT_STATUS, tech_lead_history=NO_TECH_LEAD_RUN_HISTORY,
            active_tab="kanban", e2e_status_provider=lambda _: {"enabled": False, "running": False})
        is_work = expected is IssueWorkClassification.WORK
        assert model.blocked_count == int(is_work)
        assert model.scope_summary["in_scope_total"] == 1 + int(is_work)
        assert history[0].issue_labels == ()


@pytest.mark.parametrize("at_capacity", [False, True])
def test_stale_evidence_does_not_consume_runnable_position_in_either_renderer(tmp_path, at_capacity):
    from dataclasses import replace
    import json
    from pathlib import Path
    import subprocess

    from issue_orchestrator.control.queue_cache import QueueCache
    from issue_orchestrator.domain.issue_key import FakeIssueKey
    from issue_orchestrator.domain.models import AgentConfig, Session
    from issue_orchestrator.domain.session_key import SessionKey, TaskKind
    from tests.unit.session_run_helpers import make_session_run_assets

    config = Config(repo="porchpin/porchpin")
    config.max_concurrent_sessions = 1
    state = OrchestratorState(startup_status="complete")
    cache = QueueCache(config, state)
    work = [Issue(number=number, title=f"Work {number}", labels=["agent:backend"])
            for number in (50, 51)]
    cache.replace_from_refresh([case_file(), *work])
    stale = replace(case_file(), labels=["agent:tech-lead"])
    cache.replace_from_cache([stale, *work])
    if at_capacity:
        agent = AgentConfig(prompt_path=tmp_path / "prompt.md", model="test", timeout_minutes=45)
        config.agents = {"agent:tech-lead": agent}
        state.active_sessions.append(Session(
            key=SessionKey(issue=FakeIssueKey("48"), task=TaskKind.CODE), issue=case_file(48),
            agent_config=agent, terminal_id="issue-48", worktree_path=tmp_path,
            branch_name="issue-48", run_assets=make_session_run_assets(tmp_path, session_name="issue-48"),
            started_at=datetime(2026, 9, 6),
        ))
    model = build_dashboard_view_model(OrchestratorView(state, config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS, tech_lead_history=NO_TECH_LEAD_RUN_HISTORY,
        active_tab="kanban", e2e_status_provider=lambda _: {"enabled": False, "running": False})
    expected = (["Waiting: at capacity (1/1 running)"] * 2 if at_capacity else
                ["Waiting: next scheduler tick", "Waiting: 1 runnable queued ahead"])
    assert [item["issue_number"] for item in model.queue_items] == [50, 51]
    assert [item["queue_wait_reason"] for item in model.queue_items] == expected
    assert model.queue_total == 2
    assert model.active_count == 0
    assert model.active_session_count == int(at_capacity)
    assert len(state.active_sessions) == int(at_capacity)
    assert state.cached_queue_issues == [stale, *work]
    compact = get_templates().get_template("dashboard.html").render(**model.template_context())
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const expanded = require('./src/issue_orchestrator/static/js/expanded_column_state.js');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
const context = {
    escapeHtml: escape, escapeAttr: escape, cssEscape: String, document: {},
    compactCardState: {computeCompactCardFingerprint: () => 'fingerprint'},
    formatDashboardTimestamps: () => {},
    localStorage: {getItem: () => null, setItem: () => {}},
    window: {dashboardData: {queueRefreshSeconds: 0}, location: {href: 'http://example.test/'}}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('./src/issue_orchestrator/static/js/dashboard/kanban_columns.js', 'utf8'), context);
process.stdout.write(expanded.getExpandedItemsFromViewModel(payload, 'queued')
    .map(item => context.renderExpandedCardHtml(item, 'queued', false)).join(''));
"""
    expanded = subprocess.run(["node", "-e", script], input=json.dumps(model.to_dict(), default=str),
        text=True, capture_output=True, check=True, cwd=Path(__file__).resolve().parents[2])
    for html in (compact, expanded.stdout):
        dom = BeautifulSoup(html, "html.parser")
        assert dom.select('[data-issue="49"]') == []
        for number, reason in zip((50, 51), expected, strict=True):
            card = dom.select_one(f'[data-issue="{number}"]')
            assert card is not None
            assert reason in card.get_text()


def render_historical_work_lane(model, lane, tmp_path):
    import json
    from pathlib import Path
    from tests.process_group_run import run_in_process_group

    payload = tmp_path / "history-lane.json"
    payload.write_text(json.dumps({"model": model.to_dict(), "lane": lane}, default=str))
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const expanded = require('./src/issue_orchestrator/static/js/expanded_column_state.js');
const {model, lane} = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
const context = {
    escapeHtml: escape, escapeAttr: escape, cssEscape: String, document: {},
    compactCardState: {computeCompactCardFingerprint: () => 'fingerprint'},
    formatDashboardTimestamps: () => {},
    localStorage: {getItem: () => null, setItem: () => {}},
    window: {dashboardData: {queueRefreshSeconds: 0}, location: {href: 'http://example.test/'}}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('./src/issue_orchestrator/static/js/dashboard/kanban_columns.js', 'utf8'), context);
process.stdout.write(expanded.getExpandedItemsFromViewModel(model, lane)
    .map(item => context.renderExpandedCardHtml(item, lane, false)).join(''));
"""
    result = run_in_process_group(["node", "-e", script, str(payload)], timeout=15,
        cwd=Path(__file__).resolve().parents[2])
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("status,lane", [
    ("failed", "blocked"), ("merged", "completed"), ("completed", "awaiting-merge"),
])
def test_evidence_cannot_consume_bounded_historical_work_slots(tmp_path, status, lane):
    from dataclasses import replace
    from datetime import timedelta
    from issue_orchestrator.control.queue_cache import QueueCache

    config = Config(repo="porchpin/porchpin")
    work = Issue(number=1, title="Older real work", labels=["agent:backend"])
    evidence = [case_file(number) for number in range(100, 150)]
    history = [SessionHistoryEntry(issue_number=issue.number, title=issue.title,
        agent_type="agent:backend", status=status, runtime_minutes=1,
        completed_at=datetime(2026, 9, 6, tzinfo=timezone.utc) + timedelta(minutes=index),
        pr_url=f"https://github.com/porchpin/porchpin/pull/{issue.number + 1000}")
        for index, issue in enumerate([work, *evidence])]
    state = OrchestratorState(startup_status="complete", session_history=history)
    cache = QueueCache(config, state)
    cache.replace_from_refresh([work, *evidence])
    # Fresh classification changes remain authoritative after every snapshot eviction.
    transitions = [
        (None, {1}),
        (replace(evidence[0], labels=["agent:backend"]), {1, 100}),
        (evidence[0], {1}),
        (replace(work, labels=["tech-lead-observation"]), set()),
        (work, {1}),
    ]
    field = lane.replace("-", "_")
    for observation, expected in transitions:
        if observation is not None:
            cache.upsert_refreshed_issue(observation)
        cache.replace_from_cache([])
        model = build_dashboard_view_model(OrchestratorView(state, config),
            provider_circuit=NO_PROVIDER_CIRCUIT_STATUS, tech_lead_history=NO_TECH_LEAD_RUN_HISTORY,
            active_tab="kanban", e2e_status_provider=lambda _: {"enabled": False, "running": False})
        assert {item["issue_number"] for item in getattr(model, field + "_items")} == expected
        assert getattr(model, field + "_count") == len(expected)
        assert model.scope_summary["in_scope_total"] == len(expected)
        assert model.queue_total == 0
        assert state.session_history == history
        if status != "failed":
            # Inspection retains its own latest-50 window, including all evidence.
            assert {item["issue_number"] for item in model.history_items} == set(range(100, 150))
        compact = get_templates().get_template("dashboard.html").render(**model.template_context())
        for html in (compact, render_historical_work_lane(model, lane, tmp_path)):
            dom = BeautifulSoup(html, "html.parser")
            assert {int(card["data-issue"]) for card in dom.select('[data-issue]')} == expected
