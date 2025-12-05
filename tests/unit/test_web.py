"""Tests for the web dashboard."""

import pytest
from pathlib import Path


def test_dashboard_template_has_search_input():
    """Test that the dashboard template includes the search input."""
    template_path = Path(__file__).parent.parent.parent / "src" / "issue_orchestrator" / "templates" / "dashboard.html"
    assert template_path.exists(), "Dashboard template should exist"

    content = template_path.read_text()

    # Check for search input
    assert 'id="search-input"' in content, "Template should have search input element"
    assert 'placeholder="Filter sessions by number or title..."' in content, "Search input should have placeholder"

    # Check for search container styling
    assert '.search-container' in content, "Template should have search-container CSS class"
    assert '#search-input' in content, "Template should have search-input CSS styling"

    # Check for filter functionality JavaScript
    assert 'addEventListener' in content, "Template should have event listener for search"
    assert 'searchTerm' in content, "Template should have search term variable"


def test_dashboard_template_structure():
    """Test that the dashboard template has expected structure."""
    template_path = Path(__file__).parent.parent.parent / "src" / "issue_orchestrator" / "templates" / "dashboard.html"
    content = template_path.read_text()

    # Check for key elements
    assert '<html' in content, "Should be valid HTML"
    assert 'session-card' in content, "Should have session card styling"
    assert 'focusSession' in content, "Should have focus session function"
    assert 'sessions-grid' in content, "Should have sessions grid"
