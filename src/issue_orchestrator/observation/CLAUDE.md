# Observation

**Purpose**: Fact gathering - reading external state without modifying it.

**Boundaries**:
- Read-only operations: poll GitHub, check files, detect completions
- Populates snapshots with facts for the Planner to consume
- Never takes actions - only observes and reports
- Part of the Observer phase in Observer → Planner → ActionApplier
