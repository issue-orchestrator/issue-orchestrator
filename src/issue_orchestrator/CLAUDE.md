# Orchestrator Core Development Guide

## Event System - ALWAYS Use EventName Constants

**Critical**: All events MUST use EventName constants from the catalog. Raw strings are not accepted.

### Events vs Logs

- **Events** are for machines (UI, tests, automation) - structured, stable schema
- **Logs** are for humans (developers) - can change freely

### How to Emit Events

```python
from ..events import EventName
from ..ports import TraceEvent

# CORRECT - Use EventName constant
self._events.publish(TraceEvent(
    EventName.SESSION_STARTED,  # Type-checked!
    {
        "issue_number": 123,
        "agent": "agent:developer",
        "worktree": "/path/to/worktree"
    }
))

# WRONG - Raw strings are NOT accepted
# TraceEvent("session.started", {...})  # TypeError!
```

### Event Catalog

All canonical event names are defined in `events/catalog.py`. Event names follow the format: `{domain}.{action_past_tense}`

Domains:
- `orchestrator` - Lifecycle (started, ready, idle, paused, resumed, shutdown_*)
- `tick` - Per-cycle boundaries (started, completed)
- `session` - Agent session lifecycle (started, completed, failed, timeout)
- `issue` - Issue state changes (claimed, blocked, needs_human)
- `review` - Code review events (started, approved, changes_requested)
- `rework` - Rework cycle events (started, skipped, launching)
- `transition` - State machine transitions (applied, rejected)

### Adding New Events

1. Add the EventName constant to `events/catalog.py`
2. Emit using `TraceEvent(EventName.YOUR_EVENT, {...})`
3. Events automatically flow to all registered sinks (SSE, logging)

### How Events Flow

```
Code emits event          EventSink.publish()         pluggy hooks
      |                         |                          |
      v                         v                          v
TraceEvent(EventName.X, {}) --> PluggyEventSink --> on_trace_event(event, data)
                                                          |
                                                          v
                                                   LifecycleSSEPlugin
                                                          |
                                                          v
                                                   SSE to web UI
```

### Related Files

- `events/catalog.py` - Canonical EventName constants (SOURCE OF TRUTH)
- `ports/event_sink.py` - EventSink protocol and TraceEvent dataclass
- `execution/event_sink_adapter.py` - Pluggy-backed EventSink adapter
- `execution/lifecycle_sse.py` - SSE broadcast plugin (web UI)
