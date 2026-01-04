# ADR: Dependency scoping policy (same-milestone by default with explicit escape hatch)

**Status:** Accepted  
**Date:** 2026-01-03

## Context
Issue-orchestrator supports dependencies between issues to decide what is runnable.

Original intent: dependencies should be restricted **within a milestone** to preserve simplicity and local reasoning.
However, cross-milestone dependencies can be useful for “foundation” work that multiple milestones depend upon.

We need a policy that:
- remains simple and predictable for users
- avoids accidental long dependency chains across milestones
- still allows intentional global prerequisites

## Decision
### Default rule (no configuration required)
**An issue may depend only on issues in the same milestone.**

### Escape hatch (explicit and intentional)
Cross-milestone dependencies are permitted only if the dependency issue is explicitly marked as global via one of these mechanisms:

Option A (recommended): **Foundation milestone**
- A dependency in milestone `M0` or `Foundation` may be referenced by any milestone.

Option B: **Global dependency marker**
- The dependency issue includes a marker in its body metadata, e.g.:
  `global-dep: true`

Projects may choose A or B; A is preferred because it is easy to visualize in GitHub.

### Enforcement behavior
When an issue contains an invalid cross-milestone dependency (not same milestone and not global):
- Emit a warning/event for visibility.
- Treat the dependency as **unsatisfied**, making the issue **blocked** (non-runnable) until corrected.

## Rationale
- Preserves the “local reasoning” property: you can understand a milestone’s runnability without scanning the entire repo.
- Prevents accidental coupling across milestones.
- Still supports real-world needs for shared prerequisites through an intentional mechanism.

## Consequences
### Positive
- Simpler mental model for users and agents.
- Cleaner planner logic and more predictable scheduling.
- Reduced surprise (“why is M1 blocked by M4?”).

### Negative
- Users must explicitly opt into global/foundation dependencies.
- Requires milestone naming convention (`M0`/`Foundation`) or a body marker.

## Follow-ups
- Update dependency validation logic to implement this policy.
- Surface cross-milestone invalid dependencies in Web UI (blocked reason).
- Add tests:
  - same-milestone dependency allowed
  - cross-milestone dependency rejected
  - cross-milestone dependency allowed when dependency is in `M0`/`Foundation`
  - (if implemented) cross-milestone dependency allowed when `global-dep: true` present
