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

## Architectural Patterns

### Observer → Planner → ActionApplier (Core Loop Pattern)

The orchestrator uses a strict separation of concerns in its main loop:

```
Observer (gather facts) → Planner (decide actions) → ActionApplier (execute)
```

**1. Observer Phase** - Gathers facts about system state:
- Detects session completions (completion.json files)
- Observes PR events
- Discovers new reviews to queue
- Populates snapshot with facts (e.g., `snapshot.discovered_reviews`)

**2. Planner Phase** - Decides what actions to take:
- Receives the snapshot (facts only)
- Generates `Action` objects (AddLabelAction, QueueReviewAction, etc.)
- Does NOT execute anything - only produces a list of actions
- Location: `control/planner.py`

**3. ActionApplier Phase** - Executes the actions:
- Takes the action list from Planner
- Calls adapters/repository methods to apply each action
- Location: `control/action_applier.py`

### Why This Pattern Matters

**Never bypass the pattern.** Don't call repository methods directly from:
- Completion handlers
- Event callbacks
- Observer code

Instead, have the observer populate facts, let the planner generate actions, and let the applier execute.

**Example - Adding pr-pending label when session creates a PR:**

```python
# WRONG - Direct call in completion handler
def on_session_completed(self, ...):
    self.repository_host.add_label(issue_number, "pr-pending")  # ❌

# CORRECT - Planner generates action
# In planner.py, _plan_discovered_reviews():
for review in snapshot.discovered_reviews:
    actions.append(AddLabelAction(
        issue_number=review.issue_number,
        label=labels.PR_PENDING,
        reason="session completed with PR - awaiting merge",
    ))  # ✅
```

### Related Files

- `control/planner.py` - Planner implementation
- `control/action_applier.py` - ActionApplier implementation
- `control/actions.py` - Action dataclasses (AddLabelAction, etc.)
- `control/observer.py` - Observer implementation (gathers facts)
