"""Tests for MaterializedView handling of queue.changed events."""

import pytest
from issue_orchestrator.testing.asyncdsl.materializer import MaterializedView


def test_queue_changed_adds_new_issues_with_issue_key():
    """queue.changed event should add issues keyed by issue_key (external_id)."""
    view = MaterializedView()
    assert len(view.issues) == 0

    event = {
        "event_id": 1,
        "type": "queue.changed",
        "payload": {
            "added": [
                {"number": 123, "title": "M1-001 Fix login", "issue_key": "M1-001"},
                {"number": 456, "title": "M2-042 Add auth", "issue_key": "M2-042"},
            ],
            "removed": [],
            "total": 2,
        },
    }

    view.apply_event(event, gap_check=False)

    assert "M1-001" in view.issues
    assert "M2-042" in view.issues
    assert len(view.issues) == 2


def test_queue_changed_falls_back_to_number_when_no_issue_key():
    """queue.changed without issue_key should fall back to str(number)."""
    view = MaterializedView()

    event = {
        "event_id": 1,
        "type": "queue.changed",
        "payload": {
            "added": [{"number": 123, "title": "Test issue"}],
            "removed": [],
            "total": 1,
        },
    }

    view.apply_event(event, gap_check=False)

    assert "123" in view.issues
    assert len(view.issues) == 1


def test_queue_changed_removes_issues():
    """queue.changed event should remove issues from the view."""
    from issue_orchestrator.testing.asyncdsl.models import IssueView

    view = MaterializedView()
    view.issues["M1-001"] = IssueView(issue_key="M1-001")
    view.issues["M2-042"] = IssueView(issue_key="M2-042")
    assert len(view.issues) == 2

    event = {
        "event_id": 1,
        "type": "queue.changed",
        "payload": {
            "added": [],
            "removed": [{"number": 123, "issue_key": "M1-001"}],
            "total": 1,
        },
    }

    view.apply_event(event, gap_check=False)

    assert "M1-001" not in view.issues
    assert "M2-042" in view.issues
    assert len(view.issues) == 1


def test_queue_changed_does_not_duplicate_existing():
    """queue.changed should not overwrite existing issue data."""
    from issue_orchestrator.testing.asyncdsl.models import IssueView

    view = MaterializedView()
    view.issues["M1-001"] = IssueView(issue_key="M1-001", labels={"bug", "urgent"})

    event = {
        "event_id": 1,
        "type": "queue.changed",
        "payload": {
            "added": [{"number": 123, "title": "Test issue", "issue_key": "M1-001"}],
            "removed": [],
            "total": 1,
        },
    }

    view.apply_event(event, gap_check=False)

    # Should not overwrite existing issue
    assert "M1-001" in view.issues
    assert view.issues["M1-001"].labels == {"bug", "urgent"}
