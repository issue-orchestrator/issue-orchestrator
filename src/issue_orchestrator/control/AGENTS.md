# Control

**Purpose**: Orchestration logic - the "brain" that coordinates system behavior.

**Boundaries**:
- Uses domain types and port interfaces only
- Never imports from `adapters/` or `execution/` directly
- Follows Observer → Planner → ActionApplier pattern (see ADR-0014)
- Planner generates actions, ActionApplier executes them - never bypass this
