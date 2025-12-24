# Orchestrator Core Development Guide

## Event System - ALWAYS Use Events, Not Print Statements

**Critical**: When adding visibility/logging to the orchestrator, ALWAYS emit events via the EventSink, not `print()` statements.

### Why Events?

1. **Centralized Handling**: All events flow through pluggy hooks, enabling multiple sinks (logging, SSE, IPC, metrics) without code changes
2. **Structured Data**: Events have typed data, making them parseable and queryable
3. **Toggle-able**: Sinks can be enabled/disabled per deployment (e.g., verbose logging in e2e tests)
4. **Audit Trail**: Events are automatically timestamped and can be stored

### How to Emit Events

```python
# In orchestrator or any component with access to EventSink
from ..ports.event_sink import TraceEvent

# Create and publish the event
event = TraceEvent(
    name="session.started",  # Format: domain.action
    data={
        "issue_number": 123,
        "agent": "agent:developer",
        "worktree": "/path/to/worktree"
    }
)
self._events.publish(event)
```

### Event Naming Convention

Format: `{domain}.{action}`

Domains:
- `session` - Agent session lifecycle (started, completed, failed, timeout)
- `issue` - Issue state changes (claimed, blocked, needs_human)
- `pr` - PR events (created, merged, closed)
- `review` - Code review events (requested, approved, changes_requested, escalated)
- `orchestrator` - Orchestrator state (ready, paused, resumed, state_changed)
- `validation` - Validation events (started, completed, cache_hit, cache_miss)

### How Events Flow

```
Code emits event          EventSink.publish()         pluggy hooks
      |                         |                          |
      v                         v                          v
TraceEvent(name, data) --> PluggyEventSink --> on_trace_event(event, data)
                                                    |
                            +-------------------+---+-------------------+
                            |                   |                       |
                     LifecycleIPCPlugin  LifecycleSSEPlugin  LifecycleLoggingPlugin
                            |                   |                       |
                            v                   v                       v
                       IPC socket           SSE stream           Python logger
```

### Adding New Events

1. Define the event name using the `domain.action` convention
2. Document expected data fields in `hookspec.py` under `TraceEventSpec`
3. Emit using `self._events.publish(TraceEvent(...))`
4. Events automatically flow to all registered sinks

### For E2E Tests

Events are logged to `issue_orchestrator.events` logger. To see them during tests:

```bash
# Run with verbose output
pytest tests/e2e -v -s
```

The `LifecycleLoggingPlugin` is registered by default and logs all events at INFO level.

### Related Files

- `ports/event_sink.py` - EventSink protocol and TraceEvent dataclass
- `execution/event_sink_adapter.py` - Pluggy-backed EventSink adapter
- `execution/lifecycle_logging.py` - Logging plugin that logs all events
- `execution/lifecycle_ipc.py` - IPC broadcast plugin
- `execution/lifecycle_sse.py` - SSE broadcast plugin
- `hookspec.py` - Hook specifications including on_trace_event
- `bootstrap.py` - Where plugins are registered
