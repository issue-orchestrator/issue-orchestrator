"""Issue-row timestamp rendering guardrails."""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader


TEMPLATE_DIR = Path(__file__).parent.parent.parent / "src" / "issue_orchestrator" / "templates"


def test_issue_row_timestamp_source_is_hydrated_by_dashboard_formatter() -> None:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template("issue_row.html")
    raw_timestamp = "2026-05-12T10:00:00Z"

    html = template.render(
        issue={
            "issue_number": "E2E-7",
            "title": "abc1234",
            "agent_type": "",
            "status": "passed",
            "detail_label": "",
            "action": "details",
            "action_hint": "View run details",
            "url": "",
            "issue_url": "",
            "pr_url": "",
            "time": raw_timestamp,
            "time_is_timestamp": True,
            "is_e2e": True,
            "e2e_run_id": 7,
            "open_run_command": {
                "kind": "open_e2e_run",
                "label": "Open E2E Run",
                "run_id": 7,
                "expand_run_details": False,
            },
        },
        active_tab="e2e",
    )

    soup = BeautifulSoup(html, "html.parser")
    row = soup.select_one(".issue-row")
    assert row is not None
    assert row["data-time"] == raw_timestamp
    assert "data-time-is-timestamp" not in row.attrs
    time_el = soup.select_one(".issue-time")
    assert time_el is not None
    assert time_el["data-dashboard-timestamp"] == raw_timestamp
    assert time_el["data-dashboard-timestamp-fallback"] == "-"
    assert time_el.get_text(strip=True) == raw_timestamp


def test_issue_row_runtime_label_does_not_replace_timestamp_hydration() -> None:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template("issue_row.html")
    raw_timestamp = "2026-05-12T10:00:00Z"

    html = template.render(
        issue={
            "issue_number": 4057,
            "title": "Merged issue",
            "agent_type": "web",
            "status": "merged",
            "detail_label": "Merged",
            "action": "open",
            "action_hint": "Open PR",
            "url": "https://example.test/pr/4057",
            "issue_url": "https://example.test/issues/4057",
            "pr_url": "https://example.test/pr/4057",
            "time": raw_timestamp,
            "time_is_timestamp": True,
            "runtime_label": "12 min",
        },
        active_tab="completed",
    )

    soup = BeautifulSoup(html, "html.parser")
    time_el = soup.select_one(".issue-time")
    assert time_el is not None
    assert time_el.get_text(" ", strip=True) == f"12 min · {raw_timestamp}"
    timestamp_el = time_el.select_one("[data-dashboard-timestamp]")
    assert timestamp_el is not None
    assert timestamp_el["data-dashboard-timestamp"] == raw_timestamp
    assert timestamp_el.get_text(strip=True) == raw_timestamp
