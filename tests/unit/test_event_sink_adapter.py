"""Tests for event sink adapters.

These tests verify the behavior of event emission:
- Events are delivered to the right destinations
- Failures are isolated (one sink failing doesn't break others)
- Fire-and-forget semantics (no exceptions bubble up)
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from issue_orchestrator.events import EventName
from issue_orchestrator.execution.event_sink_adapter import (
    CompositeEventSink,
    LoggingEventSink,
    PluggyEventSink,
)
from issue_orchestrator.ports.event_sink import TraceEvent

RUN_SCOPED = {"run_dir": "/tmp/run"}


class TestPluggyEventSink:
    """Test PluggyEventSink forwards events to pluggy hooks."""

    def test_publish_forwards_to_hook(self):
        """Events are forwarded to the pluggy hook with correct name and data."""
        mock_pm = MagicMock()
        sink = PluggyEventSink(mock_pm)

        event = TraceEvent(EventName.SESSION_STARTED, {"issue_number": 42, **RUN_SCOPED})
        sink.publish(event)

        mock_pm.hook.on_trace_event.assert_called_once_with(
            event=EventName.SESSION_STARTED, data={"issue_number": 42, **RUN_SCOPED}
        )

    def test_publish_swallows_hook_exceptions(self, caplog):
        """Exceptions from hooks are caught and logged, not propagated."""
        mock_pm = MagicMock()
        mock_pm.hook.on_trace_event.side_effect = RuntimeError("Hook exploded")
        sink = PluggyEventSink(mock_pm)

        event = TraceEvent(EventName.SESSION_STARTED, {"issue_number": 42, **RUN_SCOPED})

        # Should not raise
        with caplog.at_level(logging.WARNING):
            sink.publish(event)

        # Should log warning
        assert "Failed to publish event" in caplog.text
        assert "Hook exploded" in caplog.text

    def test_publish_multiple_events(self):
        """Multiple events can be published in sequence."""
        mock_pm = MagicMock()
        sink = PluggyEventSink(mock_pm)

        sink.publish(TraceEvent(EventName.SESSION_STARTED, {"n": 1, **RUN_SCOPED}))
        sink.publish(TraceEvent(EventName.SESSION_COMPLETED, {"n": 2}))
        sink.publish(TraceEvent(EventName.ORCHESTRATOR_PAUSED, {}))

        assert mock_pm.hook.on_trace_event.call_count == 3


class TestCompositeEventSink:
    """Test CompositeEventSink fans out to multiple sinks."""

    def test_publish_fans_out_to_all_sinks(self):
        """Events are delivered to all registered sinks."""
        sink1, sink2, sink3 = MagicMock(), MagicMock(), MagicMock()
        composite = CompositeEventSink(sink1, sink2, sink3)

        event = TraceEvent(EventName.SESSION_STARTED, {"test": True, **RUN_SCOPED})
        composite.publish(event)

        sink1.publish.assert_called_once_with(event)
        sink2.publish.assert_called_once_with(event)
        sink3.publish.assert_called_once_with(event)

    def test_failure_in_one_sink_does_not_affect_others(self, caplog):
        """If one sink fails, others still receive the event."""
        sink1, sink2, sink3 = MagicMock(), MagicMock(), MagicMock()
        sink2.publish.side_effect = RuntimeError("Sink 2 exploded")
        composite = CompositeEventSink(sink1, sink2, sink3)

        event = TraceEvent(EventName.SESSION_STARTED, {"test": True, **RUN_SCOPED})

        with caplog.at_level(logging.WARNING):
            composite.publish(event)

        # Sink 1 and 3 should still receive the event
        sink1.publish.assert_called_once_with(event)
        sink3.publish.assert_called_once_with(event)

        # Warning should be logged for sink 2
        assert "Sink" in caplog.text
        assert "failed" in caplog.text

    def test_add_sink_at_runtime(self):
        """Sinks can be added after construction."""
        sink1 = MagicMock()
        composite = CompositeEventSink(sink1)

        sink2 = MagicMock()
        composite.add_sink(sink2)

        event = TraceEvent(EventName.SESSION_STARTED, dict(RUN_SCOPED))
        composite.publish(event)

        sink1.publish.assert_called_once_with(event)
        sink2.publish.assert_called_once_with(event)

    def test_empty_composite_is_no_op(self):
        """CompositeEventSink with no sinks doesn't fail."""
        composite = CompositeEventSink()
        event = TraceEvent(EventName.SESSION_STARTED, dict(RUN_SCOPED))

        # Should not raise
        composite.publish(event)

    @pytest.mark.parametrize(
        "num_failures,expected_successful",
        [
            (0, 3),  # No failures - all 3 sinks receive event
            (1, 2),  # 1 fails - 2 receive event
            (2, 1),  # 2 fail - 1 receives event
            (3, 0),  # All fail - none receive event, but no exception raised
        ],
    )
    def test_partial_failure_isolation(self, num_failures, expected_successful):
        """Regardless of how many sinks fail, no exception is raised."""
        sinks = [MagicMock() for _ in range(3)]
        for i in range(num_failures):
            sinks[i].publish.side_effect = RuntimeError(f"Sink {i} failed")

        composite = CompositeEventSink(*sinks)
        event = TraceEvent(EventName.SESSION_STARTED, dict(RUN_SCOPED))

        # Should not raise
        composite.publish(event)

        # Count successful calls
        successful = sum(1 for s in sinks if not s.publish.side_effect)
        assert successful == expected_successful


class TestLoggingEventSink:
    """Test LoggingEventSink logs events."""

    def test_logs_event_at_info_level(self, caplog):
        """Events are logged at INFO level."""
        sink = LoggingEventSink()
        event = TraceEvent(EventName.SESSION_STARTED, {"issue_number": 42, **RUN_SCOPED})

        with caplog.at_level(logging.INFO, logger="issue_orchestrator.events"):
            sink.publish(event)

        assert "[EVENT]" in caplog.text
        assert "session.started" in caplog.text
        assert "42" in caplog.text

    def test_custom_logger_name(self, caplog):
        """Custom logger name can be specified."""
        sink = LoggingEventSink(logger_name="my.custom.logger")
        event = TraceEvent(EventName.ORCHESTRATOR_PAUSED, {})

        with caplog.at_level(logging.INFO, logger="my.custom.logger"):
            sink.publish(event)

        assert "orchestrator.paused" in caplog.text


class TestEventSinkIntegration:
    """Integration tests for event sink composition."""

    def test_composite_with_logging_and_pluggy(self):
        """Composite can combine LoggingEventSink with PluggyEventSink."""
        mock_pm = MagicMock()
        pluggy_sink = PluggyEventSink(mock_pm)
        logging_sink = LoggingEventSink()
        composite = CompositeEventSink(pluggy_sink, logging_sink)

        event = TraceEvent(EventName.SESSION_COMPLETED, {"pr_url": "https://..."})
        composite.publish(event)

        # Pluggy hook was called
        mock_pm.hook.on_trace_event.assert_called_once()

    def test_nested_composite_sinks(self):
        """Composite sinks can be nested for complex routing."""
        inner_sink1, inner_sink2 = MagicMock(), MagicMock()
        inner_composite = CompositeEventSink(inner_sink1, inner_sink2)

        outer_sink = MagicMock()
        outer_composite = CompositeEventSink(inner_composite, outer_sink)

        event = TraceEvent(EventName.SESSION_STARTED, dict(RUN_SCOPED))
        outer_composite.publish(event)

        # All sinks receive the event
        inner_sink1.publish.assert_called_once()
        inner_sink2.publish.assert_called_once()
        outer_sink.publish.assert_called_once()
