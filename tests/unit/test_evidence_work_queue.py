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
