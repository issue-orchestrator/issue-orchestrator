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

## Public Contract Schemas (UI + SSE)

UI-facing payloads are centrally defined and schema-validated.

**Source of truth**
- `contracts/public.py` (Pydantic contracts)

**Generated artifacts**
- `contracts/public/*.json` (regenerate with `python scripts/generate_public_contracts.py`)

**Drift test**
- `tests/unit/test_public_contract_schemas.py`

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

## Component Ownership & Encapsulation

### The Orchestrator Coordinates, It Doesn't Execute

The orchestrator is a **coordinator**, not an executor. It should NOT:
- Delete files directly
- Call adapter methods directly (except through ActionApplier)
- Reach into other components' internals (e.g., `controller.completion_processor.cleanup()`)

Instead, components own their own lifecycle:
- `completion_processor` owns completion.json files → it handles cleanup internally
- `worktree_manager` owns worktrees → it handles creation and cleanup
- `session_launcher` owns session creation → it handles session setup

### DRY: Data Lives in One Place

Don't pass data that's already available:
- If `Session` has `completion_path`, don't also pass it as a separate parameter
- If a component needs data, pass the object that contains it

```python
# WRONG - Passing data that's already in the session
decide_outcome(obs, session.worktree_path, session.issue.number,
               session.issue.title, session.tmux_session_name, session.completion_path)

# BETTER - Pass the session object
decide_outcome(obs, session)
```

### No Quick Fixes - Fix the Design, Not the Symptom

When fixing bugs:
1. Understand the root cause, not just the symptom
2. Fix at the right abstraction level
3. Don't add code that reaches across boundaries
4. If the fix feels hacky, step back and reconsider

**CRITICAL: Never apply band-aid fixes.** If a bug reveals a design flaw:
- Fix the design, don't patch around it
- A senior developer fixes architecture; a junior developer subtracts 1

Example of a BAD fix:
```python
# Bug: review sessions looked up by issue.number fail
# BAD FIX: Just use tmux_session_name instead
exists = self._session_exists_by_name(session.tmux_session_name)  # ❌ Band-aid!
```

Example of a GOOD fix:
```python
# GOOD FIX: Introduce proper session identity abstraction
# Session has key: SessionKey (domain identity)
# Session has terminal_id: str (opaque, adapter interprets)
exists = self._session_runner.session_exists(session.terminal_id)  # ✅ Proper abstraction
```

## Abstraction Heuristics

- Favor higher-level abstractions when they improve clarity, conciseness, or testability.
- If callers must rummage across disparate classes/fields to accomplish a task, consider introducing a higher-level port or helper.
- Entry points should depend on behavior-level ports, not storage or transport details.

## Strongly Typed Run-Asset Ownership

Active session, completion, and review-exchange paths must preserve explicit
typed data flow for run artifacts.

- The owner that creates a session run owns filesystem allocation/discovery.
  Lower layers receive typed values via constructor or method arguments.
- Active `Session` creation requires a frozen typed run contract such as
  `SessionRunAssets`; do not pass a naked `Path`, optional, string, default, or
  rediscoverable hint where a real run contract is required.
- Leaf functions should declare the narrowest typed artifact they need. Group
  paths into frozen aggregates only when the aggregate proves an invariant, such
  as several files belonging to the same run directory and session identity.
- No active path may use "latest run", completion-path inference, alternate
  names, session-name search, worktree scans, or similar fallback recovery for a
  missing `run_dir`.
- If the owner cannot satisfy the contract, fail fast. Historical/UI inspection
  may be best-effort, but it must stay outside active control flow and be named
  as inspection.
- Avoid weak metadata maps for owned artifacts. Prefer frozen dataclasses,
  enums, value objects, and required constructor arguments over `dict[str, str]`,
  sentinel values, or optional required fields.
- Tests must inject typed run assets directly. Fakes should fail if an active
  path attempts fallback discovery.

## Hexagonal Architecture - Layer Boundaries

This codebase follows hexagonal (ports & adapters) architecture. **Respect layer boundaries.**

## GitHub Adapter Guardrail

All GitHub API calls must go through `execution/github_adapter.py` / `execution/github_http.py`. Direct gh CLI usage and imports of `_github_impl` are forbidden. This enforces caching, auditing, and rate-limit discipline in one place.

### Layer Responsibilities

| Layer | Location | Knows About | Never Knows About |
|-------|----------|-------------|-------------------|
| Domain | `domain/`, `models.py` | SessionKey, IssueKey, TaskKind, business rules | GitHub, tmux, file paths |
| Control | `control/`, `orchestrator.py` | Domain types + opaque IDs (terminal_id) | How terminal_id is encoded |
| Ports | `ports/` | Abstract interfaces | Implementations |
| Adapters | `execution/` | Port interfaces + external systems | Domain business logic |

### Domain Purity

The domain layer must be **infrastructure-agnostic**:

```python
# WRONG - Domain knows about tmux
class Session:
    tmux_session_name: str  # ❌ Infrastructure leaked into domain!

# CORRECT - Domain uses abstractions
class Session:
    key: SessionKey      # ✅ Domain identity
    terminal_id: str     # ✅ Opaque handle, adapter interprets
```

### Identity Abstractions

Use proper identity types, not primitive obsession:

| Concept | Identity Type | NOT |
|---------|---------------|-----|
| Session slot | `SessionKey` | `tmux_session_name`, `issue.number` |
| Issue reference | `IssueKey` | `issue_number: int` |
| Task type | `TaskKind` enum | `"review"`, `"code"` strings |

```python
@dataclass(frozen=True)
class SessionKey:
    """Slot identity for a session. Domain concept."""
    issue: IssueKey
    task: TaskKind

    def stable_id(self) -> str:
        return f"{self.task.value}:{self.issue.stable_id()}"
```

### Terminal ID is Opaque

The `terminal_id` field is an **opaque string** that only the terminal adapter interprets:

- Domain/Control: stores and passes `terminal_id`, never parses it
- Adapter: encodes/decodes `terminal_id` ↔ terminal-specific names

```python
# Control layer - treats terminal_id as opaque
def check_session(self, session: Session) -> bool:
    return self._session_runner.exists(session.terminal_id)  # Just pass it

# Adapter layer - knows the encoding
class TmuxAdapter:
    def exists(self, terminal_id: str) -> bool:
        # Adapter knows terminal_id is a tmux window name
        return self._manager.window_exists_by_name(terminal_id)
```

This allows swapping terminal implementations without touching domain/control code.

### Smell Detection

If you see these, stop and refactor:

| Smell | Problem | Fix |
|-------|---------|-----|
| `tmux_` prefix in domain | Infrastructure leak | Use opaque `terminal_id` |
| Parsing session names in control | Control knows too much | Move parsing to adapter |
| `issue.number` for session lookup | Wrong identity | Use `SessionKey` or `terminal_id` |
| String literals for task types | No type safety | Use `TaskKind` enum |
