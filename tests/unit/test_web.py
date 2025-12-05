"""Tests for web dashboard."""

from pathlib import Path

from issue_orchestrator.web import get_templates


def test_dashboard_template_has_status_filter():
    """Test that the dashboard template includes status filter dropdown."""
    templates = get_templates()
    template = templates.get_template("dashboard.html")

    # Render with some test data
    html = template.render(
        sessions=[
            {
                "issue_number": 123,
                "title": "Test issue",
                "runtime_minutes": 5,
                "agent_type": "agent:frontend",
                "status": "running",
            },
            {
                "issue_number": 456,
                "title": "Slow issue",
                "runtime_minutes": 20,
                "agent_type": "agent:backend",
                "status": "slow",
            },
        ],
        paused=False,
        max_sessions=2,
        completed_count=0,
        queue_count=0,
    )

    # Verify filter dropdown exists
    assert 'id="statusFilter"' in html
    assert 'Filter by status:' in html
    assert '<option value="all">All Sessions</option>' in html
    assert '<option value="running">Running</option>' in html
    assert '<option value="slow">Slow</option>' in html

    # Verify filter function exists
    assert 'function filterSessions()' in html
    assert 'onchange="filterSessions()"' in html

    # Verify CSS classes for filtering exist
    assert '.session-card.hidden' in html


def test_dashboard_template_without_sessions():
    """Test that filter is not shown when there are no sessions."""
    templates = get_templates()
    template = templates.get_template("dashboard.html")

    html = template.render(
        sessions=[],
        paused=False,
        max_sessions=2,
        completed_count=0,
        queue_count=0,
    )

    # Filter should not be shown when there are no sessions
    assert 'id="statusFilter"' not in html
    assert 'Filter by status:' not in html
