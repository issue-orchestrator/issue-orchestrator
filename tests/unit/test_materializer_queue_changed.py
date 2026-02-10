"""Tests for MaterializedView handling of queue.changed events."""

from issue_orchestrator.testing.asyncdsl.materializer import MaterializedView


def test_queue_changed_adds_new_issues():
    """queue.changed event should add new issues to the view."""
    view = MaterializedView()
    assert len(view.issues) == 0

    event = {
        "event_id": 1,
        "type": "queue.changed",
        "payload": {
            "added": [
                {"number": 123, "title": "Test issue 123"},
                {"number": 456, "title": "Test issue 456"},
            ],
            "removed": [],
            "total": 2,
        },
    }

    view.apply_event(event, gap_check=False)

    assert "123" in view.issues
    assert "456" in view.issues
    assert len(view.issues) == 2


def test_queue_changed_removes_issues():
    """queue.changed event should remove issues from the view."""
    view = MaterializedView()
    # Pre-populate with issues
    view.issues["123"] = view.issues.get("123") or type(  # type: ignore
        "IssueView", (), {"issue_key": "123", "labels": set()}
    )()
    view.issues["456"] = view.issues.get("456") or type(  # type: ignore
        "IssueView", (), {"issue_key": "456", "labels": set()}
    )()
    assert len(view.issues) == 2

    event = {
        "event_id": 1,
        "type": "queue.changed",
        "payload": {
            "added": [],
            "removed": [{"number": 123}],
            "total": 1,
        },
    }

    view.apply_event(event, gap_check=False)

    assert "123" not in view.issues
    assert "456" in view.issues
    assert len(view.issues) == 1


def test_queue_changed_does_not_duplicate_existing():
    """queue.changed should not overwrite existing issue data."""
    from issue_orchestrator.testing.asyncdsl.models import IssueView

    view = MaterializedView()
    # Pre-populate with issue that has labels
    view.issues["123"] = IssueView(issue_key="123", labels={"bug", "urgent"})

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

    # Should not overwrite existing issue
    assert "123" in view.issues
    assert view.issues["123"].labels == {"bug", "urgent"}
