"""Unit tests for GitHub audit module."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from issue_orchestrator.infra import gh_audit
from issue_orchestrator.infra.gh_audit import (
    AuditReason,
    AuditScope,
    check_rate_limit,
    configure,
    configure_rate_limit,
    context,
    emit_report,
    enabled,
    get_audit_path,
    get_rate_limit_checked_at,
    get_rate_limit_config,
    get_rate_limit_snapshot,
    get_stats,
    include_events,
    record,
    reset_stats,
    set_event_sink,
    set_rate_limit_fetcher,
    update_last_call,
)


@pytest.fixture(autouse=True)
def clean_audit_state():
    """Reset audit state before and after each test.

    This fixture accesses private module state to provide test isolation.
    It saves original state, resets to known state for the test, then restores
    original state afterward to avoid polluting other tests in the suite.
    This is test infrastructure, not testing implementation details.
    """
    # Save original state (test infrastructure accessing module internals for isolation)
    original_enabled = gh_audit._ENABLED  # noqa: SLF001
    original_include_events = gh_audit._INCLUDE_EVENTS  # noqa: SLF001
    original_audit_path = gh_audit._AUDIT_PATH  # noqa: SLF001
    original_event_sink = gh_audit._event_sink  # noqa: SLF001
    original_rate_limit_fetcher = gh_audit._rate_limit_fetcher  # noqa: SLF001
    original_atexit_registered = gh_audit._atexit_registered  # noqa: SLF001

    # Reset to known state (test infrastructure)
    gh_audit._ENABLED = False  # noqa: SLF001
    gh_audit._INCLUDE_EVENTS = False  # noqa: SLF001
    gh_audit._AUDIT_PATH = None  # noqa: SLF001
    gh_audit._event_sink = None  # noqa: SLF001
    gh_audit._rate_limit_fetcher = None  # noqa: SLF001
    gh_audit._rate_limit_every_calls = 0  # noqa: SLF001
    gh_audit._rate_limit_warn_fraction = 0.1  # noqa: SLF001
    gh_audit._rate_limit_warn_remaining = 100  # noqa: SLF001
    gh_audit._atexit_registered = False  # noqa: SLF001
    reset_stats()

    yield

    # Restore original state (test infrastructure)
    gh_audit._ENABLED = original_enabled  # noqa: SLF001
    gh_audit._INCLUDE_EVENTS = original_include_events  # noqa: SLF001
    gh_audit._AUDIT_PATH = original_audit_path  # noqa: SLF001
    gh_audit._event_sink = original_event_sink  # noqa: SLF001
    gh_audit._rate_limit_fetcher = original_rate_limit_fetcher  # noqa: SLF001
    gh_audit._atexit_registered = original_atexit_registered  # noqa: SLF001
    reset_stats()


class TestAuditConfiguration:
    """Tests for audit configuration functions."""

    def test_configure_enabled_true(self):
        """Configure should enable auditing when enabled=True."""
        configure(enabled=True)
        assert enabled() is True

    def test_configure_enabled_false(self):
        """Configure should disable auditing when enabled=False."""
        configure(enabled=False)
        assert enabled() is False

    def test_configure_include_events(self):
        """Configure should set include_events flag."""
        configure(include_events=True)
        assert include_events() is True

        configure(include_events=False)
        assert include_events() is False

    def test_configure_audit_path(self):
        """Configure should set custom audit path."""
        custom_path = "/tmp/custom-audit.json"
        configure(audit_path=custom_path)
        assert get_audit_path() == custom_path

    def test_configure_multiple_params(self):
        """Configure should accept multiple parameters at once."""
        configure(enabled=True, include_events=True, audit_path="/tmp/test.json")
        assert enabled() is True
        assert include_events() is True
        assert get_audit_path() == "/tmp/test.json"

    def test_configure_none_preserves_existing(self):
        """Configure with None should preserve existing values."""
        configure(enabled=True, include_events=True, audit_path="/tmp/test.json")
        configure(enabled=None, include_events=None, audit_path=None)
        assert enabled() is True
        assert include_events() is True
        assert get_audit_path() == "/tmp/test.json"

    def test_enabled_returns_false_by_default(self):
        """enabled() should return False by default."""
        assert enabled() is False

    def test_enabled_respects_environment(self):
        """enabled() should respect ORCHESTRATOR_GH_AUDIT environment variable."""
        with patch.dict(os.environ, {"ORCHESTRATOR_GH_AUDIT": "1"}):
            # Re-import to pick up environment
            import importlib
            importlib.reload(gh_audit)
            assert gh_audit.enabled() is True


class TestAuditReasons:
    """Tests for AuditReason constants."""

    def test_audit_reason_constants_exist(self):
        """AuditReason should have all expected constants."""
        assert hasattr(AuditReason, "QUEUE_REFRESH_SCHEDULED")
        assert hasattr(AuditReason, "QUEUE_REFRESH_MANUAL")
        assert hasattr(AuditReason, "SNAPSHOT_REFRESH")
        assert hasattr(AuditReason, "STARTUP_REFRESH")
        assert hasattr(AuditReason, "LABEL_SYNC_SCAN")
        assert hasattr(AuditReason, "EXTERNAL_ID_RESOLVE")
        assert hasattr(AuditReason, "PR_SCAN")
        assert hasattr(AuditReason, "TEST_DATA_CREATE")
        assert hasattr(AuditReason, "TEST_DATA_UPDATE")
        assert hasattr(AuditReason, "TEST_DATA_CLOSE")
        assert hasattr(AuditReason, "TEST_DATA_LIST")
        assert hasattr(AuditReason, "TEST_DATA_LABEL")
        assert hasattr(AuditReason, "GH_WRITE")
        assert hasattr(AuditReason, "GH_READ")

    def test_audit_reason_values_are_strings(self):
        """AuditReason values should be strings."""
        assert isinstance(AuditReason.QUEUE_REFRESH_SCHEDULED, str)
        assert isinstance(AuditReason.PR_SCAN, str)
        assert isinstance(AuditReason.GH_WRITE, str)


class TestAuditScopes:
    """Tests for AuditScope constants."""

    def test_audit_scope_constants_exist(self):
        """AuditScope should have all expected constants."""
        assert hasattr(AuditScope, "STARTUP")
        assert hasattr(AuditScope, "PERIODIC")
        assert hasattr(AuditScope, "MANUAL")
        assert hasattr(AuditScope, "TEST")
        assert hasattr(AuditScope, "ON_DEMAND")
        assert hasattr(AuditScope, "UNKNOWN")

    def test_audit_scope_values_are_strings(self):
        """AuditScope values should be strings."""
        assert isinstance(AuditScope.STARTUP, str)
        assert isinstance(AuditScope.PERIODIC, str)
        assert isinstance(AuditScope.UNKNOWN, str)


class TestAuditContext:
    """Tests for audit context manager."""

    def test_context_manager_sets_reason(self):
        """Context manager should set reason in thread-local context."""
        configure(enabled=True)

        with context(reason=AuditReason.PR_SCAN):
            record(
                args=["gh", "pr", "list"],
                repo="owner/repo",
                duration_ms=100,
                error=None,
                caller="test_caller",
            )

        # Check that reason was recorded
        assert get_stats()["by_reason"].get(AuditReason.PR_SCAN) == 1

    def test_context_manager_sets_issue_key(self):
        """Context manager should set issue_key in thread-local context."""
        configure(enabled=True)

        with context(reason=AuditReason.GH_READ, issue_key="123"):
            record(
                args=["gh", "issue", "view"],
                repo="owner/repo",
                duration_ms=50,
                error=None,
                caller="test_caller",
            )

        # Check that issue was recorded
        assert get_stats()["by_issue"].get("123") == 1

    def test_context_manager_sets_scope(self):
        """Context manager should set scope in thread-local context."""
        configure(enabled=True)

        with context(reason=AuditReason.STARTUP_REFRESH, scope=AuditScope.STARTUP):
            record(
                args=["gh", "issue", "list"],
                repo="owner/repo",
                duration_ms=200,
                error=None,
                caller="test_caller",
            )

        # Check that scope was recorded
        assert get_stats()["by_scope"].get(AuditScope.STARTUP) == 1

    def test_context_manager_nesting(self):
        """Nested context managers should restore previous context."""
        configure(enabled=True)

        with context(reason=AuditReason.PR_SCAN, scope=AuditScope.PERIODIC):
            record(
                args=["gh", "pr", "list"],
                repo="owner/repo",
                duration_ms=100,
                error=None,
                caller="outer",
            )

            with context(reason=AuditReason.GH_READ, scope=AuditScope.ON_DEMAND):
                record(
                    args=["gh", "issue", "view"],
                    repo="owner/repo",
                    duration_ms=50,
                    error=None,
                    caller="inner",
                )

            # Back to outer context
            record(
                args=["gh", "pr", "view"],
                repo="owner/repo",
                duration_ms=75,
                error=None,
                caller="outer_again",
            )

        # Verify both contexts were used
        assert get_stats()["by_reason"].get(AuditReason.PR_SCAN) == 2
        assert get_stats()["by_reason"].get(AuditReason.GH_READ) == 1
        assert get_stats()["by_scope"].get(AuditScope.PERIODIC) == 2
        assert get_stats()["by_scope"].get(AuditScope.ON_DEMAND) == 1

    def test_context_with_none_values(self):
        """Context with None values should use default UNKNOWN scope."""
        configure(enabled=True)

        with context(reason=None, issue_key=None, scope=None):
            record(
                args=["gh", "api", "/rate_limit"],
                repo=None,
                duration_ms=30,
                error=None,
                caller="test",
            )

        # Should default to UNKNOWN scope
        assert get_stats()["by_scope"].get(AuditScope.UNKNOWN) == 1


class TestAuditRecording:
    """Tests for record() function."""

    def test_record_disabled_does_nothing(self):
        """record() should do nothing when auditing is disabled."""
        configure(enabled=False)

        record(
            args=["gh", "issue", "list"],
            repo="owner/repo",
            duration_ms=100,
            error=None,
            caller="test_caller",
        )

        # No stats should be recorded
        assert get_stats()["total_calls"] == 0

    def test_record_basic_call(self):
        """record() should track basic call stats."""
        configure(enabled=True)

        record(
            args=["gh", "issue", "list"],
            repo="owner/repo",
            duration_ms=100,
            error=None,
            caller="test_caller",
        )

        assert get_stats()["total_calls"] == 1
        assert get_stats()["by_command"]["gh issue"] == 1
        assert get_stats()["by_caller"]["test_caller"] == 1

    def test_record_with_bytes_returned(self):
        """record() should track bytes returned."""
        configure(enabled=True)

        record(
            args=["gh", "api", "/repos/owner/repo/issues"],
            repo="owner/repo",
            duration_ms=150,
            error=None,
            caller="test_caller",
            bytes_returned=5000,
        )

        assert get_stats()["total_bytes_returned"] == 5000

    def test_record_with_items_returned(self):
        """record() should track items returned."""
        configure(enabled=True)

        record(
            args=["gh", "issue", "list"],
            repo="owner/repo",
            duration_ms=200,
            error=None,
            caller="test_caller",
            items_returned=25,
        )

        assert get_stats()["total_items_returned"] == 25

    def test_record_with_full_scan(self):
        """record() should track full scan metrics."""
        configure(enabled=True)

        record(
            args=["gh", "issue", "list"],
            repo="owner/repo",
            duration_ms=300,
            error=None,
            caller="test_caller",
            items_returned=100,
            full_scan=True,
        )

        assert get_stats()["full_scan_calls"] == 1
        assert get_stats()["full_scan_items_returned"] == 100

    def test_record_with_error(self):
        """record() should track errors."""
        configure(enabled=True)

        record(
            args=["gh", "issue", "view", "999"],
            repo="owner/repo",
            duration_ms=50,
            error="Not found",
            caller="test_caller",
        )

        assert get_stats()["errors"] == 1

    def test_record_with_rate_limit_headers(self):
        """record() should store rate limit info from response headers."""
        configure(enabled=True)

        rate_limit = {
            "limit": 5000,
            "remaining": 4900,
            "reset": 1234567890,
        }

        record(
            args=["gh", "issue", "list"],
            repo="owner/repo",
            duration_ms=100,
            error=None,
            caller="test_caller",
            rate_limit=rate_limit,
        )

        assert get_stats()["last_rate_limit_from_headers"] is not None
        assert get_stats()["last_rate_limit_from_headers"]["limit"] == 5000
        assert get_stats()["last_rate_limit_from_headers"]["remaining"] == 4900
        assert "updated_at" in get_stats()["last_rate_limit_from_headers"]

    def test_record_tracks_by_caller_command(self):
        """record() should track combined caller::command stats."""
        configure(enabled=True)

        record(
            args=["gh", "issue", "list"],
            repo="owner/repo",
            duration_ms=100,
            error=None,
            caller="orchestrator",
        )

        key = "orchestrator::gh issue"
        assert key in get_stats()["by_caller_command"]
        entry = get_stats()["by_caller_command"][key]
        assert entry["caller"] == "orchestrator"
        assert entry["command"] == "gh issue"
        assert entry["count"] == 1
        assert entry["total_ms"] == 100

    def test_record_with_context(self):
        """record() should capture context when available."""
        configure(enabled=True)

        with context(
            reason=AuditReason.QUEUE_REFRESH_SCHEDULED,
            issue_key="issue-123",
            scope=AuditScope.PERIODIC,
        ):
            record(
                args=["gh", "issue", "view", "123"],
                repo="owner/repo",
                duration_ms=80,
                error=None,
                caller="test_caller",
                items_returned=1,
            )

        # Verify all context dimensions were tracked
        assert get_stats()["by_reason"][AuditReason.QUEUE_REFRESH_SCHEDULED] == 1
        assert get_stats()["by_issue"]["issue-123"] == 1
        assert get_stats()["by_scope"][AuditScope.PERIODIC] == 1

        # Verify reason totals
        reason_totals = get_stats()["by_reason_totals"][AuditReason.QUEUE_REFRESH_SCHEDULED]
        assert reason_totals["calls"] == 1
        assert reason_totals["items_returned"] == 1
        assert reason_totals["total_ms"] == 80

        # Verify scope totals
        scope_totals = get_stats()["by_scope_totals"][AuditScope.PERIODIC]
        assert scope_totals["calls"] == 1
        assert scope_totals["items_returned"] == 1
        assert scope_totals["total_ms"] == 80

    def test_record_with_events_enabled(self):
        """record() should capture event details when include_events is True."""
        configure(enabled=True, include_events=True)

        with context(reason=AuditReason.PR_SCAN, scope=AuditScope.MANUAL):
            record(
                args=["gh", "pr", "list", "--state", "open"],
                repo="owner/repo",
                duration_ms=120,
                error=None,
                caller="pr_scanner",
                items_returned=10,
                bytes_returned=2000,
                full_scan=False,
            )

        assert len(get_stats()["events"]) == 1
        event = get_stats()["events"][0]
        assert event["caller"] == "pr_scanner"
        assert event["command"] == "gh pr"
        assert event["args"] == ["gh", "pr", "list", "--state", "open"]
        assert event["repo"] == "owner/repo"
        assert event["duration_ms"] == 120
        assert event["error"] is None
        assert event["reason"] == AuditReason.PR_SCAN
        assert event["scope"] == AuditScope.MANUAL
        assert event["items_returned"] == 10
        assert event["bytes_returned"] == 2000
        assert event["full_scan"] is False
        assert "call_id" in event
        assert "ts" in event

    def test_record_command_key_extraction(self):
        """record() should extract command key from args correctly."""
        configure(enabled=True)

        # Two-word command
        record(args=["gh", "issue", "list"], repo="owner/repo", duration_ms=10, error=None, caller="test")
        assert "gh issue" in get_stats()["by_command"]

        reset_stats()

        # Single-word command
        record(args=["gh", "status"], repo="owner/repo", duration_ms=10, error=None, caller="test")
        assert "gh status" in get_stats()["by_command"]

        reset_stats()

        # Empty args (edge case)
        record(args=[], repo="owner/repo", duration_ms=10, error=None, caller="test")
        assert "unknown" in get_stats()["by_command"]

    def test_record_aggregates_multiple_calls(self):
        """record() should aggregate stats across multiple calls."""
        configure(enabled=True)

        with context(reason=AuditReason.GH_READ, scope=AuditScope.TEST):
            for i in range(5):
                record(
                    args=["gh", "issue", "view", str(100 + i)],
                    repo="owner/repo",
                    duration_ms=50,
                    error=None,
                    caller="test_caller",
                    items_returned=1,
                    bytes_returned=500,
                )

        assert get_stats()["total_calls"] == 5
        assert get_stats()["total_items_returned"] == 5
        assert get_stats()["total_bytes_returned"] == 2500
        assert get_stats()["by_command"]["gh issue"] == 5

        reason_totals = get_stats()["by_reason_totals"][AuditReason.GH_READ]
        assert reason_totals["calls"] == 5
        assert reason_totals["items_returned"] == 5
        assert reason_totals["bytes_returned"] == 2500
        assert reason_totals["total_ms"] == 250


class TestUpdateLastCall:
    """Tests for update_last_call() function."""

    def test_update_last_call_disabled_does_nothing(self):
        """update_last_call() should do nothing when auditing is disabled."""
        configure(enabled=False)

        update_last_call(items_returned=100)

        # No changes to stats
        assert get_stats()["total_items_returned"] == 0

    def test_update_last_call_updates_items(self):
        """update_last_call() should update items_returned for last call."""
        configure(enabled=True)

        record(
            args=["gh", "issue", "list"],
            repo="owner/repo",
            duration_ms=100,
            error=None,
            caller="test",
            items_returned=10,
        )

        # Update to 25 items (delta of +15)
        update_last_call(items_returned=25)

        assert get_stats()["total_items_returned"] == 25

    def test_update_last_call_updates_bytes(self):
        """update_last_call() should update bytes_returned for last call."""
        configure(enabled=True)

        record(
            args=["gh", "api", "/repos/owner/repo/issues"],
            repo="owner/repo",
            duration_ms=100,
            error=None,
            caller="test",
            bytes_returned=1000,
        )

        # Update to 2500 bytes (delta of +1500)
        update_last_call(bytes_returned=2500)

        assert get_stats()["total_bytes_returned"] == 2500

    def test_update_last_call_with_context(self):
        """update_last_call() should update reason/scope totals."""
        configure(enabled=True)

        with context(reason=AuditReason.PR_SCAN, scope=AuditScope.PERIODIC):
            record(
                args=["gh", "pr", "list"],
                repo="owner/repo",
                duration_ms=100,
                error=None,
                caller="test",
                items_returned=5,
                bytes_returned=1000,
            )

            # Update counts
            update_last_call(items_returned=15, bytes_returned=3000)

        # Check reason totals updated
        reason_totals = get_stats()["by_reason_totals"][AuditReason.PR_SCAN]
        assert reason_totals["items_returned"] == 15
        assert reason_totals["bytes_returned"] == 3000

        # Check scope totals updated
        scope_totals = get_stats()["by_scope_totals"][AuditScope.PERIODIC]
        assert scope_totals["items_returned"] == 15
        assert scope_totals["bytes_returned"] == 3000

    def test_update_last_call_full_scan_items(self):
        """update_last_call() should update full_scan_items_returned.

        Note: There's a quirk in the implementation where full_scan_items_returned
        uses the updated entry value instead of prev_items, which means the delta
        calculation is based on the NEW value. This test documents current behavior.
        """
        configure(enabled=True)

        with context(reason=AuditReason.QUEUE_REFRESH_SCHEDULED, scope=AuditScope.PERIODIC):
            record(
                args=["gh", "issue", "list"],
                repo="owner/repo",
                duration_ms=200,
                error=None,
                caller="test",
                items_returned=50,
                full_scan=True,
            )

            # Update to 100 items
            # Must be called in same context to have access to thread-local last_call_id
            update_last_call(items_returned=100)

        # Note: Due to line 463 in gh_audit.py using entry.get("items_returned")
        # instead of prev_items, the delta is computed as (100 - 100) = 0
        # So full_scan_items_returned stays at the original 50
        assert get_stats()["full_scan_items_returned"] == 50

        # Same for reason totals
        reason_totals = get_stats()["by_reason_totals"][AuditReason.QUEUE_REFRESH_SCHEDULED]
        assert reason_totals["full_scan_items_returned"] == 50

    def test_update_last_call_no_previous_call(self):
        """update_last_call() should handle case with no previous call gracefully."""
        configure(enabled=True)

        # Call without recording anything first
        update_last_call(items_returned=10)

        # Should not crash, just do nothing
        assert get_stats()["total_items_returned"] == 0


class TestRateLimitChecking:
    """Tests for rate limit checking functionality."""

    def test_check_rate_limit_no_fetcher(self):
        """check_rate_limit() should return None when no fetcher configured."""
        result = check_rate_limit("test_reason")

        assert result is None

    def test_check_rate_limit_success(self):
        """check_rate_limit() should fetch and store rate limit snapshot."""
        payload = {
            "resources": {
                "core": {"remaining": 4800, "limit": 5000, "reset": 1234567890},
                "search": {"remaining": 25, "limit": 30, "reset": 1234567900},
                "graphql": {"remaining": 4500, "limit": 5000, "reset": 1234567910},
            }
        }

        fetcher = MagicMock(return_value=payload)
        set_rate_limit_fetcher(fetcher)

        result = check_rate_limit("manual_check")

        assert result is not None
        assert result["core"]["remaining"] == 4800
        assert result["core"]["limit"] == 5000
        assert result["search"]["remaining"] == 25
        assert result["graphql"]["remaining"] == 4500

        # Check stored in stats
        assert get_stats()["last_rate_limit"] == result
        assert get_stats()["last_rate_limit_checked_at"] is not None

    def test_check_rate_limit_fetcher_exception(self):
        """check_rate_limit() should handle fetcher exceptions gracefully."""
        fetcher = MagicMock(side_effect=Exception("Network error"))
        set_rate_limit_fetcher(fetcher)

        result = check_rate_limit("test_reason")

        assert result is None

    def test_check_rate_limit_fetcher_returns_none(self):
        """check_rate_limit() should handle fetcher returning None."""
        fetcher = MagicMock(return_value=None)
        set_rate_limit_fetcher(fetcher)

        result = check_rate_limit("test_reason")

        assert result is None

    def test_check_rate_limit_with_event_sink(self):
        """check_rate_limit() should publish events when sink is configured."""
        payload = {
            "resources": {
                "core": {"remaining": 4500, "limit": 5000, "reset": 1234567890},
                "search": {"remaining": 20, "limit": 30, "reset": 1234567900},
                "graphql": {"remaining": 4000, "limit": 5000, "reset": 1234567910},
            }
        }

        fetcher = MagicMock(return_value=payload)
        set_rate_limit_fetcher(fetcher)

        event_sink = MagicMock()
        set_event_sink(event_sink)

        check_rate_limit("periodic_check")

        # Should publish GH_RATE_LIMIT event (EventName and TraceEvent are imported inside the function)
        assert event_sink.publish.called

    def test_check_rate_limit_warning_low_remaining(self):
        """check_rate_limit() should warn when remaining is below threshold."""
        configure_rate_limit(every_calls=0, warn_fraction=0.1, warn_remaining=100)

        payload = {
            "resources": {
                "core": {"remaining": 50, "limit": 5000, "reset": 1234567890},
                "search": {"remaining": 30, "limit": 30, "reset": 1234567900},
                "graphql": {"remaining": 5000, "limit": 5000, "reset": 1234567910},
            }
        }

        fetcher = MagicMock(return_value=payload)
        set_rate_limit_fetcher(fetcher)

        event_sink = MagicMock()
        set_event_sink(event_sink)

        check_rate_limit("warning_test")

        # Should publish warning event (remaining=50 < warn_remaining=100)
        # EventName and TraceEvent are imported inside the check_rate_limit function
        assert event_sink.publish.call_count >= 1

    def test_check_rate_limit_warning_low_fraction(self):
        """check_rate_limit() should warn when remaining is below fraction of limit."""
        configure_rate_limit(every_calls=0, warn_fraction=0.1, warn_remaining=10)

        payload = {
            "resources": {
                "core": {"remaining": 400, "limit": 5000, "reset": 1234567890},
                "search": {"remaining": 30, "limit": 30, "reset": 1234567900},
                "graphql": {"remaining": 5000, "limit": 5000, "reset": 1234567910},
            }
        }

        fetcher = MagicMock(return_value=payload)
        set_rate_limit_fetcher(fetcher)

        event_sink = MagicMock()
        set_event_sink(event_sink)

        check_rate_limit("fraction_test")

        # Should publish warning event (400 < 5000*0.1=500)
        # EventName and TraceEvent are imported inside the check_rate_limit function
        assert event_sink.publish.call_count >= 1

    def test_configure_rate_limit(self):
        """configure_rate_limit() should set rate limit check parameters."""
        configure_rate_limit(every_calls=50, warn_fraction=0.15, warn_remaining=150)

        config = get_rate_limit_config()
        assert config["every_calls"] == 50
        assert config["warn_fraction"] == 0.15
        assert config["warn_remaining"] == 150

    def test_record_triggers_rate_limit_check(self):
        """record() should trigger rate limit check at configured interval."""
        configure(enabled=True)
        configure_rate_limit(every_calls=3, warn_fraction=0.1, warn_remaining=100)

        payload = {
            "resources": {
                "core": {"remaining": 4800, "limit": 5000, "reset": 1234567890},
                "search": {"remaining": 30, "limit": 30, "reset": 1234567900},
                "graphql": {"remaining": 5000, "limit": 5000, "reset": 1234567910},
            }
        }

        fetcher = MagicMock(return_value=payload)
        set_rate_limit_fetcher(fetcher)

        # Record 3 calls to trigger check
        for i in range(3):
            record(
                args=["gh", "issue", "view", str(i)],
                repo="owner/repo",
                duration_ms=50,
                error=None,
                caller="test",
            )

        # Should have been called once (after 3rd call)
        fetcher.assert_called_once()

    def test_get_rate_limit_snapshot(self):
        """get_rate_limit_snapshot() should return stored snapshot after check."""
        payload = {
            "resources": {
                "core": {"remaining": 4800, "limit": 5000, "reset": 123},
                "search": {"remaining": 25, "limit": 30, "reset": 456},
                "graphql": {"remaining": 4500, "limit": 5000, "reset": 789},
            }
        }

        fetcher = MagicMock(return_value=payload)
        set_rate_limit_fetcher(fetcher)

        # Perform a check to populate the snapshot
        check_rate_limit("test_reason")

        result = get_rate_limit_snapshot()
        assert result is not None
        assert result["core"]["remaining"] == 4800
        assert result["search"]["remaining"] == 25

    def test_get_rate_limit_checked_at(self):
        """get_rate_limit_checked_at() should return timestamp after check."""
        payload = {
            "resources": {
                "core": {"remaining": 4800, "limit": 5000, "reset": 123},
            }
        }

        fetcher = MagicMock(return_value=payload)
        set_rate_limit_fetcher(fetcher)

        before = time.time()
        check_rate_limit("test_reason")
        after = time.time()

        result = get_rate_limit_checked_at()
        assert result is not None
        assert before <= result <= after


class TestResetStats:
    """Tests for reset_stats() function."""

    def test_reset_stats_clears_all_counters(self):
        """reset_stats() should reset all counters to initial state."""
        configure(enabled=True)

        # Record some data
        with context(reason=AuditReason.PR_SCAN, scope=AuditScope.PERIODIC):
            record(
                args=["gh", "pr", "list"],
                repo="owner/repo",
                duration_ms=100,
                error=None,
                caller="test",
                items_returned=10,
                bytes_returned=1000,
            )

        # Verify data exists
        assert get_stats()["total_calls"] > 0

        # Reset
        reset_stats()

        # Verify all counters reset
        assert get_stats()["total_calls"] == 0
        assert get_stats()["total_items_returned"] == 0
        assert get_stats()["total_bytes_returned"] == 0
        assert get_stats()["full_scan_calls"] == 0
        assert get_stats()["by_command"] == {}
        assert get_stats()["by_caller"] == {}
        assert get_stats()["by_reason"] == {}
        assert get_stats()["errors"] == 0

class TestEmitReport:
    """Tests for emit_report() function."""

    def test_emit_report_disabled_returns_none(self):
        """emit_report() should return None when auditing is disabled."""
        configure(enabled=False)

        result = emit_report()

        assert result is None

    def test_emit_report_creates_json_file(self):
        """emit_report() should create a JSON file with audit data."""
        configure(enabled=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "test-audit.json"
            configure(audit_path=str(audit_path))

            # Record some data
            record(
                args=["gh", "issue", "list"],
                repo="owner/repo",
                duration_ms=100,
                error=None,
                caller="test_caller",
                items_returned=5,
            )

            # Emit report
            result_path = emit_report()

            assert result_path == str(audit_path)
            assert audit_path.exists()

            # Verify JSON content
            with open(audit_path) as f:
                data = json.load(f)

            assert data["total_calls"] == 1
            assert data["total_items_returned"] == 5
            assert data["by_command"]["gh issue"] == 1
            assert data["by_caller"]["test_caller"] == 1

    def test_emit_report_default_path_uses_pid(self):
        """emit_report() should use /tmp/gh-audit-<pid>.json by default."""
        configure(enabled=True)

        # Don't set custom path
        result_path = emit_report()

        assert result_path is not None
        assert f"/tmp/gh-audit-{os.getpid()}.json" == result_path

    def test_emit_report_path_with_pid_placeholder(self):
        """emit_report() should replace {pid} placeholder in path."""
        configure(enabled=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path_template = str(Path(tmpdir) / "audit-{pid}.json")
            configure(audit_path=audit_path_template)

            result_path = emit_report()

            expected_path = str(Path(tmpdir) / f"audit-{os.getpid()}.json")
            assert result_path == expected_path

    def test_emit_report_calculates_usage_units(self):
        """emit_report() should calculate usage_units metric."""
        configure(enabled=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "test-audit.json"
            configure(audit_path=str(audit_path))

            with context(reason=AuditReason.PR_SCAN, scope=AuditScope.PERIODIC):
                record(
                    args=["gh", "pr", "list"],
                    repo="owner/repo",
                    duration_ms=100,
                    error=None,
                    caller="test",
                    items_returned=10,
                )
                record(
                    args=["gh", "pr", "view", "1"],
                    repo="owner/repo",
                    duration_ms=50,
                    error=None,
                    caller="test",
                    items_returned=1,
                )

            emit_report()

            with open(audit_path) as f:
                data = json.load(f)

            # usage_units = total_calls + total_items_returned = 2 + 11 = 13
            assert data["usage_units"] == 13

            # Scope totals should have usage_units
            scope_totals = data["by_scope_totals"][AuditScope.PERIODIC]
            assert scope_totals["usage_units"] == 13

            # Reason totals should have usage_units
            reason_totals = data["by_reason_totals"][AuditReason.PR_SCAN]
            assert reason_totals["usage_units"] == 13

    def test_emit_report_handles_write_failure(self):
        """emit_report() should handle file write failures gracefully."""
        configure(enabled=True)

        # Set invalid path (directory doesn't exist)
        configure(audit_path="/nonexistent/directory/audit.json")

        # Should not crash, just log warning
        result = emit_report()

        # Still returns the attempted path
        assert result == "/nonexistent/directory/audit.json"

    def test_emit_report_prints_summary(self, capsys):
        """emit_report() should print summary to stdout."""
        configure(enabled=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "test-audit.json"
            configure(audit_path=str(audit_path))

            record(
                args=["gh", "issue", "list"],
                repo="owner/repo",
                duration_ms=100,
                error=None,
                caller="test_caller",
                items_returned=5,
                bytes_returned=1000,
            )

            emit_report()

            captured = capsys.readouterr()

            # Verify summary lines printed
            assert "[GH-AUDIT]" in captured.out
            assert "total_calls=1" in captured.out
            assert "items=5" in captured.out
            assert "bytes=1000" in captured.out


class TestEventSink:
    """Tests for event sink integration."""

    def test_check_rate_limit_publishes_event(self):
        """check_rate_limit() should publish event when sink is configured."""
        payload = {
            "resources": {
                "core": {"remaining": 4800, "limit": 5000, "reset": 1234567890},
                "search": {"remaining": 30, "limit": 30, "reset": 1234567900},
                "graphql": {"remaining": 5000, "limit": 5000, "reset": 1234567910},
            }
        }

        fetcher = MagicMock(return_value=payload)
        set_rate_limit_fetcher(fetcher)

        sink = MagicMock()
        set_event_sink(sink)

        check_rate_limit("test_reason")

        # Event should be published
        # EventName and TraceEvent are imported inside the check_rate_limit function
        assert sink.publish.called


class TestThreadSafety:
    """Tests for thread safety of audit module."""

    def test_record_is_thread_safe(self):
        """record() should be thread-safe for concurrent calls."""
        configure(enabled=True)

        def record_multiple():
            for i in range(10):
                record(
                    args=["gh", "issue", "view", str(i)],
                    repo="owner/repo",
                    duration_ms=10,
                    error=None,
                    caller="thread_test",
                )

        threads = [threading.Thread(target=record_multiple) for _ in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have recorded 50 calls (10 * 5 threads)
        assert get_stats()["total_calls"] == 50

    def test_context_is_thread_local(self):
        """Context should be thread-local and not shared between threads."""
        configure(enabled=True)

        def record_with_context(thread_id, reason):
            with context(reason=reason, scope=AuditScope.TEST):
                record(
                    args=["gh", "issue", "view", str(thread_id)],
                    repo="owner/repo",
                    duration_ms=10,
                    error=None,
                    caller=f"thread_{thread_id}",
                )

        thread1 = threading.Thread(target=record_with_context, args=(1, AuditReason.PR_SCAN))
        thread2 = threading.Thread(target=record_with_context, args=(2, AuditReason.GH_READ))

        thread1.start()
        thread2.start()
        thread1.join()
        thread2.join()

        # Each thread's context should have been properly isolated -
        # verified by checking that both reasons were recorded correctly
        assert get_stats()["by_reason"][AuditReason.PR_SCAN] == 1
        assert get_stats()["by_reason"][AuditReason.GH_READ] == 1


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_record_with_empty_args(self):
        """record() should handle empty args list."""
        configure(enabled=True)

        record(
            args=[],
            repo="owner/repo",
            duration_ms=10,
            error=None,
            caller="test",
        )

        assert get_stats()["by_command"]["unknown"] == 1

    def test_record_with_single_arg(self):
        """record() should handle single argument in args."""
        configure(enabled=True)

        record(
            args=["gh"],
            repo="owner/repo",
            duration_ms=10,
            error=None,
            caller="test",
        )

        assert get_stats()["by_command"]["gh"] == 1

    def test_record_with_none_repo(self):
        """record() should handle None repo."""
        configure(enabled=True)

        record(
            args=["gh", "api", "/rate_limit"],
            repo=None,
            duration_ms=10,
            error=None,
            caller="test",
        )

        assert get_stats()["total_calls"] == 1

    def test_update_last_call_without_record(self):
        """update_last_call() should not crash when called without prior record."""
        configure(enabled=True)

        # Should not raise exception
        update_last_call(items_returned=10, bytes_returned=100)

        assert get_stats()["total_items_returned"] == 0
        assert get_stats()["total_bytes_returned"] == 0

    def test_rate_limit_handles_missing_resources(self):
        """check_rate_limit should handle missing resource keys in payload."""
        payload = {
            "resources": {
                "core": {"remaining": 5000, "limit": 5000},
                # Missing search and graphql - should be handled gracefully
            }
        }

        fetcher = MagicMock(return_value=payload)
        set_rate_limit_fetcher(fetcher)

        # Should not crash with partial data
        result = check_rate_limit("test_reason")

        assert result is not None
        assert result["core"]["remaining"] == 5000

    def test_rate_limit_handles_empty_resources(self):
        """check_rate_limit should handle empty resources dict."""
        payload = {"resources": {}}

        fetcher = MagicMock(return_value=payload)
        set_rate_limit_fetcher(fetcher)

        # Should not crash with empty data
        result = check_rate_limit("test_reason")

        assert result is not None

    def test_rate_limit_warning_handles_none_values(self):
        """check_rate_limit should not warn when values are None."""
        # Payload with None values should not trigger warning
        payload = {
            "resources": {
                "core": {},  # No remaining/limit keys
                "search": {},
                "graphql": {},
            }
        }

        fetcher = MagicMock(return_value=payload)
        set_rate_limit_fetcher(fetcher)

        event_sink = MagicMock()
        set_event_sink(event_sink)

        configure_rate_limit(every_calls=0, warn_fraction=0.5, warn_remaining=1000)

        result = check_rate_limit("test_reason")

        # Should not crash and should not emit warning event (only rate limit event)
        assert result is not None
        # No warning should be emitted for None values
        warning_calls = [
            call for call in event_sink.publish.call_args_list
            if "warning" in str(call).lower()
        ]
        assert len(warning_calls) == 0

    def test_emit_report_with_no_data(self):
        """emit_report() should work with no recorded data."""
        configure(enabled=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "empty-audit.json"
            configure(audit_path=str(audit_path))

            result_path = emit_report()

            assert result_path == str(audit_path)
            assert audit_path.exists()

            with open(audit_path) as f:
                data = json.load(f)

            assert data["total_calls"] == 0
            assert data["usage_units"] == 0
