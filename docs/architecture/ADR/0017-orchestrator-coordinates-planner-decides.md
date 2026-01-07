# ADR 0017: Orchestrator coordinates, Planner decides

**Status:** Accepted
**Date:** 2024-12-21

## Context

The orchestrator could be implemented as a monolithic controller that:
- Gathers facts
- Makes decisions
- Executes actions

This leads to a "god object" that's hard to test, reason about, and modify.

## Decision

**The orchestrator is a coordinator, not a decision-maker. Policy decisions live in the Planner.**

### Responsibility Split

| Component | Responsibility | Does NOT |
|-----------|---------------|----------|
| **Orchestrator** | Coordinates the loop, delegates to components | Make policy decisions |
| **FactGatherer** | Creates snapshot of current state | Decide or execute |
| **Planner** | Decides what actions to take given facts | Execute actions |
| **ActionApplier** | Executes actions via adapters | Make decisions |

### Orchestrator's Role

```python
class Orchestrator:
    def tick(self):
        # 1. Coordinate fact gathering
        snapshot = self.fact_gatherer.create_snapshot()

        # 2. Delegate decisions to planner
        actions = self.planner.plan(snapshot)

        # 3. Coordinate execution
        self.action_applier.apply_all(actions)
```

The orchestrator:
- Owns the main loop
- Wires components together
- Handles lifecycle (start, stop, pause)
- Does NOT contain `if/else` policy logic

### Planner's Role

```python
class Planner:
    def plan(self, snapshot: Snapshot) -> list[Action]:
        actions = []

        # Policy decisions live HERE
        if snapshot.has_pending_completions:
            actions.extend(self._plan_completions(snapshot))

        if self._should_launch_sessions(snapshot):
            actions.extend(self._plan_session_launches(snapshot))

        return actions
```

The planner:
- Contains all policy logic
- Is pure (no side effects)
- Can be unit tested with fake snapshots
- Returns actions, never executes

## Consequences

### Positive
- **Testable**: Planner tested with synthetic snapshots
- **Readable**: Policy logic concentrated in one place
- **Modifiable**: Change policy without touching coordinator
- **Debuggable**: Log snapshot + actions to understand decisions

### Negative
- More indirection than monolithic approach
- Must pass context through snapshot
- Action types must be predefined

## Anti-Pattern

```python
# WRONG - Orchestrator making policy decisions
class Orchestrator:
    def tick(self):
        if len(self.active_sessions) < self.max_concurrent:  # ❌ Policy in orchestrator
            issue = self._pick_next_issue()  # ❌ Decision logic here
            self._launch_session(issue)  # ❌ Direct execution
```

## Related

- ADR-0014: Observer → Planner → Apply loop pattern
- `control/planner.py`: Planner implementation
