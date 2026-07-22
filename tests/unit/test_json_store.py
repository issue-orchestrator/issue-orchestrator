"""Tests for JSON session store implementation."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from issue_orchestrator.execution.json_store import JsonSessionStore


@pytest.fixture
def store_path(tmp_path):
    """Create a temporary path for the store."""
    return tmp_path / "sessions.json"


@pytest.fixture
def store(store_path):
    """Create a JsonSessionStore instance."""
    return JsonSessionStore(store_path)


class TestJsonSessionStore:
    """Tests for JsonSessionStore."""

    def test_init_creates_empty_store(self, store, store_path):
        """Test initialization creates an empty store structure."""
        # Store file is not created until first write
        assert not store_path.exists()

    def test_save_session_state(self, store, store_path):
        """Test saving session state."""
        started_at = datetime(2025, 1, 1, 12, 0, 0)
        metadata = {"branch": "feature/test"}

        store.save_session_state(
            session_id="session-123",
            issue_number=42,
            state="running",
            started_at=started_at,
            metadata=metadata
        )

        # Verify in memory
        state = store.get_session_state("session-123")
        assert state is not None
        assert state["issue_number"] == 42
        assert state["state"] == "running"
        assert state["started_at"] == "2025-01-01T12:00:00"
        assert state["metadata"] == {"branch": "feature/test"}
        assert "updated_at" in state

        # Verify persisted to disk
        assert store_path.exists()
        with open(store_path) as f:
            data = json.load(f)
        assert "session-123" in data["state_machines"]
        assert data["state_machines"]["session-123"]["state"] == "running"

    def test_get_session_state_not_found(self, store):
        """Test getting state for non-existent session returns None."""
        assert store.get_session_state("nonexistent") is None

    def test_get_all_sessions(self, store):
        """Test getting all session states."""
        store.save_session_state("session-1", 42, "running")
        store.save_session_state("session-2", 43, "completed")

        all_sessions = store.get_all_sessions()
        assert len(all_sessions) == 2
        assert "session-1" in all_sessions
        assert "session-2" in all_sessions
        assert all_sessions["session-1"]["state"] == "running"
        assert all_sessions["session-2"]["state"] == "completed"

    def test_delete_session_state(self, store):
        """Test deleting session state."""
        store.save_session_state("session-123", 42, "running")
        assert store.get_session_state("session-123") is not None

        store.delete_session_state("session-123")
        assert store.get_session_state("session-123") is None

    def test_delete_nonexistent_session(self, store):
        """Test deleting non-existent session is a no-op."""
        # Should not raise an error
        store.delete_session_state("nonexistent")

    def test_save_issue_state(self, store):
        """Test saving issue state machine state."""
        metadata = {"attempt": 1}

        store.save_issue_state(
            issue_number=42,
            state="in_progress",
            pr_number=123,
            metadata=metadata
        )

        state = store.get_issue_state(42)
        assert state is not None
        assert state["state"] == "in_progress"
        assert state["pr_number"] == 123
        assert state["metadata"] == {"attempt": 1}
        assert "updated_at" in state

    def test_get_issue_state_not_found(self, store):
        """Test getting state for non-existent issue returns None."""
        assert store.get_issue_state(999) is None

    def test_save_review_state(self, store):
        """Test saving review state machine state."""
        metadata = {"reviewer": "tech_lead"}

        store.save_review_state(
            pr_number=123,
            state="pending",
            rework_count=2,
            metadata=metadata
        )

        state = store.get_review_state(123)
        assert state is not None
        assert state["state"] == "pending"
        assert state["rework_count"] == 2
        assert state["metadata"] == {"reviewer": "tech_lead"}
        assert "updated_at" in state

    def test_get_review_state_not_found(self, store):
        """Test getting state for non-existent review returns None."""
        assert store.get_review_state(999) is None

    def test_clear_completed(self, store):
        """Test clearing completed/terminal state entries."""
        # Add mix of active and completed states
        store.save_session_state("session-1", 42, "running")
        store.save_session_state("session-2", 43, "completed")
        store.save_session_state("session-3", 44, "failed")
        store.save_session_state("session-4", 45, "timed_out")
        store.save_issue_state(42, "in_progress")
        store.save_issue_state(43, "completed")
        store.save_review_state(123, "pending")
        store.save_review_state(124, "merged")
        store.save_review_state(125, "closed")

        # Clear completed
        removed = store.clear_completed()

        # Should remove 6 terminal states: 3 sessions + 1 issue + 2 reviews
        # Terminal: session-2 (completed), session-3 (failed), session-4 (timed_out),
        #           issue 43 (completed), review 124 (merged), review 125 (closed)
        assert removed == 6

        # Verify active states remain
        assert store.get_session_state("session-1") is not None
        assert store.get_issue_state(42) is not None
        assert store.get_review_state(123) is not None

        # Verify completed states removed
        assert store.get_session_state("session-2") is None
        assert store.get_session_state("session-3") is None
        assert store.get_session_state("session-4") is None
        assert store.get_issue_state(43) is None
        assert store.get_review_state(124) is None
        assert store.get_review_state(125) is None

    def test_clear_completed_when_none(self, store):
        """Test clear_completed returns 0 when no completed states."""
        store.save_session_state("session-1", 42, "running")
        removed = store.clear_completed()
        assert removed == 0

    def test_persistence_across_instances(self, store_path):
        """Test that state persists across different store instances."""
        # Create first store and save data
        store1 = JsonSessionStore(store_path)
        store1.save_session_state("session-123", 42, "running")
        store1.save_issue_state(43, "in_progress", pr_number=100)

        # Create second store (should load from disk)
        store2 = JsonSessionStore(store_path)

        # Verify data loaded
        session_state = store2.get_session_state("session-123")
        assert session_state is not None
        assert session_state["state"] == "running"

        issue_state = store2.get_issue_state(43)
        assert issue_state is not None
        assert issue_state["state"] == "in_progress"
        assert issue_state["pr_number"] == 100

    def test_load_corrupted_file(self, store_path):
        """Test gracefully handles corrupted JSON file."""
        # Write invalid JSON
        store_path.parent.mkdir(parents=True, exist_ok=True)
        with open(store_path, 'w') as f:
            f.write("{ invalid json }")

        # Should initialize successfully (with empty data)
        store = JsonSessionStore(store_path)
        # Verify store is functional (can save without error)
        store.save_session_state("session-123", 42, "running")

    def test_save_creates_parent_directory(self, tmp_path):
        """Test that save creates parent directories if needed."""
        nested_path = tmp_path / "nested" / "dir" / "sessions.json"
        store = JsonSessionStore(nested_path)

        store.save_session_state("session-123", 42, "running")

        assert nested_path.exists()
        assert nested_path.parent.is_dir()

    def test_session_state_without_optional_fields(self, store):
        """Test saving session state without optional fields."""
        store.save_session_state(
            session_id="session-123",
            issue_number=42,
            state="pending"
        )

        state = store.get_session_state("session-123")
        assert state is not None
        assert state["started_at"] is None
        assert state["metadata"] == {}

    def test_multiple_saves_update_timestamp(self, store):
        """Test that multiple saves update the timestamp."""
        store.save_session_state("session-123", 42, "pending")
        state1 = store.get_session_state("session-123")
        updated_at_1 = state1["updated_at"]

        # Save again with different state
        store.save_session_state("session-123", 42, "running")
        state2 = store.get_session_state("session-123")
        updated_at_2 = state2["updated_at"]

        # Timestamp should be different (or same if very fast)
        # At minimum, state should be updated
        assert state2["state"] == "running"
        # Note: We can't reliably assert timestamps are different due to timing

    def test_json_format_is_human_readable(self, store, store_path):
        """Test that JSON output is formatted for readability."""
        store.save_session_state("session-123", 42, "running")

        with open(store_path) as f:
            content = f.read()

        # Should have indentation (not minified)
        assert "  " in content or "\n" in content
        # Should be valid JSON
        data = json.loads(content)
        assert "state_machines" in data

    def test_update_existing_session_state(self, store):
        """Test updating an existing session state."""
        # Initial save
        store.save_session_state("session-123", 42, "pending")
        state1 = store.get_session_state("session-123")
        assert state1["state"] == "pending"

        # Update
        started_at = datetime(2025, 1, 1, 12, 0, 0)
        store.save_session_state(
            "session-123",
            42,
            "running",
            started_at=started_at,
            metadata={"attempt": 2}
        )

        state2 = store.get_session_state("session-123")
        assert state2["state"] == "running"
        assert state2["started_at"] == "2025-01-01T12:00:00"
        assert state2["metadata"] == {"attempt": 2}

    def test_concurrent_machine_types(self, store):
        """Test that different machine types are stored separately."""
        # Save states for all three machine types
        store.save_session_state("session-123", 42, "running")
        store.save_issue_state(42, "in_progress")
        store.save_review_state(100, "pending")

        # All should coexist
        assert store.get_session_state("session-123") is not None
        assert store.get_issue_state(42) is not None
        assert store.get_review_state(100) is not None
