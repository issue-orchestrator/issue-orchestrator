# ADR 0014: Observer → Planner → ActionApplier loop pattern

**Status:** Accepted
**Date:** 2024-12-21

## Context

The orchestrator's main loop must:
- Gather facts about current state (GitHub, sessions, completions)
- Decide what actions to take
- Execute those actions
- Handle failures gracefully

Mixing these concerns leads to:
- Untestable code (side effects interleaved with logic)
- Race conditions (state changes during decision-making)
- Unclear responsibility (who decides vs who executes?)

## Decision

**Strict three-phase loop: Observe → Plan → Apply**

### Phase 1: Observe (gather facts)

```python
# FactGatherer creates immutable snapshot
snapshot = fact_gatherer.create_snapshot()
# Contains: issues, PRs, labels, sessions, completions
# NO decisions, NO side effects
```

- `FactGatherer` gathers facts about system state
- Creates immutable `Snapshot` object
- Detects session completions, PR events, reviews to queue
- **Never** mutates state or calls adapters for writes

### Phase 2: Plan (decide actions)

```python
# Planner produces action list from snapshot
actions = planner.plan(snapshot)
# Returns: [AddLabelAction, LaunchSessionAction, ...]
# NO execution, NO side effects
```

- `Planner` receives snapshot (facts only)
- Generates `Action` objects describing what should happen
- Pure logic - can be unit tested with fake snapshots
- **Never** executes actions or calls adapters

### Phase 3: Apply (execute actions)

```python
# ActionApplier executes via ports
results = action_applier.apply_all(actions)
# Calls adapters, emits events
```

- `ActionApplier` takes action list from Planner
- Executes each action via appropriate port/adapter
- Records results, emits trace events
- **Never** makes policy decisions

## Consequences

### Positive
- **Testable**: Each phase tested in isolation
- **Auditable**: Actions logged before execution
- **Predictable**: Snapshot is frozen during planning
- **Debuggable**: Can inspect snapshot + actions without executing

### Negative
- More ceremony than direct calls
- Snapshot may be stale by apply time (mitigated by ADR-0007)
- Action types must be defined upfront

## Anti-Patterns (DON'T DO THIS)

```python
# WRONG - Direct call in completion handler
def on_session_completed(self, ...):
    self.repository_host.add_label(issue, "pr-pending")  # ❌

# WRONG - Decision in observer
def observe(self):
    if should_launch:  # ❌ Decision in observer
        self.launch_session()  # ❌ Execution in observer
```

## Related

- ADR-0007: Verify state before mutation (stale snapshot mitigation)
- `control/planner.py`, `control/action_applier.py`, `control/fact_gatherer.py`
