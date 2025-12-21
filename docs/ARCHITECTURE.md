# Architecture

This document describes the architectural principles and directory structure of the issue-orchestrator.

## Core Principle

**Components that observe are Observers; components that decide are Controllers; components that act are Adapters.**

This separation creates clear responsibility boundaries:

| Layer | Responsibility | Authority |
|-------|----------------|-----------|
| **Observation** | Gather facts about current state | None - just reports |
| **Control** | Make decisions, advance state | Full authority |
| **Execution** | Perform actions on external systems | None - just executes |

## Directory Structure

```
src/issue_orchestrator/
├── control/              # Authority/decision layer
│   ├── scheduler.py      # Decides which issues to work on
│   └── (lifecycle.py)    # Future: LifecycleController
│
├── observation/          # Fact-gathering layer
│   └── observer.py       # SessionObserver - observes session state
│
├── execution/            # Action layer (adapters)
│   ├── github_adapter.py # Talks to GitHub API
│   ├── terminal_tmux.py  # Controls tmux sessions
│   ├── terminal_iterm.py # Controls iTerm2 sessions
│   ├── json_store.py     # Persists session data
│   └── manager.py        # Plugin manager
│
├── ports/                # Interfaces (protocols)
│   ├── issue_tracker.py     # IssueTracker protocol
│   ├── pull_request_tracker.py  # PullRequestTracker protocol
│   ├── label_set.py         # LabelSet protocol
│   ├── working_copy.py      # WorkingCopy protocol (local git)
│   └── session_store.py     # SessionStore protocol
│
├── domain/               # Domain models and state machines
│   ├── events.py         # Domain events
│   └── state_machines/   # State machine implementations
│
└── orchestrator.py       # Main facade (delegates to control/)
```

## Control Plane vs Execution Plane

### Control Plane (Authority)
- Lives in `control/` and `orchestrator.py`
- Makes policy decisions
- Advances state machines
- Determines what actions to take
- Does NOT directly call external systems

### Execution Plane (Mechanics)
- Lives in `execution/`
- Talks to external systems
- Returns facts/results
- Does NOT make policy decisions
- Just does what it's told

### Observation Layer (Facts)
- Lives in `observation/`
- Gathers facts about current state
- Reports what IS, not what SHOULD BE
- Does NOT make policy decisions
- Does NOT mutate state

## Naming Conventions

### Interfaces (Ports)
- **IssueTracker** - not "IssueRepository" (implies external system, not storage)
- **PullRequestTracker** - not "PRRepository"
- **LabelSet** - not "LabelManager" (avoids policy implication)
- **WorkingCopy** - not "GitRepository" (neutral, SCM-agnostic)

### Classes
- **SessionObserver** - not "SessionMonitor" (observers observe, don't control)
- **Scheduler** - makes scheduling decisions (control plane)
- **GitHubAdapter** - implements platform protocols (execution plane)

## The CompletionRecord Pattern

When an agent completes work, it writes a `CompletionRecord` to JSON:

```python
@dataclass
class CompletionRecord:
    session_id: str           # Orchestrator's session identifier
    outcome: CompletionOutcome  # What happened (COMPLETED, BLOCKED, etc.)
    requested_actions: list[RequestedAction]  # What agent wants
    # ... status-specific fields
```

Key principle: **The agent reports intent; the orchestrator decides and executes.**

1. Agent writes JSON completion record (observation)
2. Orchestrator reads and validates (untrusted input!)
3. Orchestrator decides what to do (control)
4. Orchestrator executes via adapters (execution)

## Hexagonal Architecture

The system follows hexagonal (ports and adapters) architecture:

```
                    ┌─────────────────────┐
                    │                     │
    ┌───────────────┤   Control Plane     ├───────────────┐
    │               │   (Orchestrator)    │               │
    │               │                     │               │
    │               └──────────┬──────────┘               │
    │                          │                          │
    ▼                          ▼                          ▼
┌───────┐               ┌─────────────┐              ┌────────┐
│ Ports │◄──────────────┤   Domain    ├──────────────► Ports │
│(Input)│               │  (Models,   │               │(Output)│
└───┬───┘               │   Events)   │               └───┬────┘
    │                   └─────────────┘                   │
    ▼                                                     ▼
┌───────────┐                                       ┌───────────┐
│ Adapters  │                                       │ Adapters  │
│(Execution)│                                       │(Execution)│
└───────────┘                                       └───────────┘
    │                                                     │
    ▼                                                     ▼
┌───────────┐                                       ┌───────────┐
│  GitHub   │                                       │  Terminal │
│   API     │                                       │  (tmux)   │
└───────────┘                                       └───────────┘
```

## Dependency Injection

The orchestrator uses constructor-based dependency injection:

```python
@dataclass
class Orchestrator:
    config: Config
    events: EventSink = field(default_factory=NullEventSink)
    runner: SessionRunner = field(default_factory=NullSessionRunner)
    _github_adapter: Optional[GitHubAdapter] = field(default=None)
```

### Key Ports

| Port | Purpose | Production Adapter |
|------|---------|-------------------|
| `EventSink` | Fire-and-forget trace events | `PluggyEventSink` |
| `SessionRunner` | Terminal session management | `PluggySessionRunner` |
| `IssueTracker` | Issue operations | `GitHubAdapter` |
| `SessionStore` | Persist session state | `JsonSessionStore` |

### Composition Root

`bootstrap.py` is the **only place** that wires dependencies:

```python
def build_orchestrator(config: Config) -> Orchestrator:
    pm = create_plugin_manager(...)  # Pluggy stays here
    events = PluggyEventSink(pm)
    runner = PluggySessionRunner(pm)
    github = GitHubAdapter(config.repo)
    return Orchestrator(config=config, events=events, runner=runner, _github_adapter=github)
```

### Why This Matters

- **Core has no pluggy imports** - orchestrator.py only knows about Protocols
- **Testable** - inject MockEventSink, MockSessionRunner in tests
- **Extensible** - swap adapters without touching core logic

## Testing

The architecture enables clean testing:

1. **Unit tests** - Mock ports, test control logic in isolation
2. **Integration tests** - Use real adapters, verify wiring
3. **Contract tests** - Verify adapters implement protocols correctly

### Test Fixtures

```python
# conftest.py provides auto-patching
@pytest.fixture(autouse=True)
def patch_orchestrator_dependencies(monkeypatch):
    """Injects MockEventSink and MockSessionRunner into all Orchestrator instances."""
    # Patches __post_init__ to inject mocks
```

Example:
```python
# Control plane test - mock the port
def test_scheduler_prioritizes_by_milestone():
    mock_tracker = MockIssueTracker()
    mock_tracker.issues = [...]

    scheduler = Scheduler(config)
    result = scheduler.get_next_issues(mock_tracker)

    assert result[0].number == 42  # Highest priority
```

## Backwards Compatibility

To minimize churn, backwards compatibility modules exist:

- `issue_orchestrator.observer` → re-exports from `observation/`
- `issue_orchestrator.scheduler` → re-exports from `control/`
- `issue_orchestrator.adapters` → re-exports from `execution/`

New code should import from the canonical locations.
