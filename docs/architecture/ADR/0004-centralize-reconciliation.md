# ADR 0004: Centralize reconciliation (startup + runtime) behind a single entrypoint

**Status:** Accepted  
**Date:** 2025-12-31

## Context
Failures can occur at any step:
- crash between write steps (labels set but PR not created)
- partial writes (comment posted, label not updated)
- external actors modify issues/labels/PRs
- stale local worktrees/sessions exist

If reconciliation logic is spread across:
- startup routines
- tick loops
- random helper methods
then correctness becomes non-local and brittle.

We also need to distinguish:
- **startup reconciliation** (recover from last run)
- **runtime reconciliation** (guard each apply and detect drift)

## Decision
Provide a single reconciliation entrypoint:

`reconcile(observations, local_state) -> ReconcileResult`

Used in four places:
1. **Startup**: before processing any work (recover / repair / label drift).
2. **Before apply**: confirm preconditions still hold.
3. **After apply**: confirm writes were observed; detect partial completion.
4. **On completion**: when agent/session indicates done (consume completion observation).

The reconcile function:
- identifies drift (violations between expected and observed state)
- performs only **safe** repairs automatically (idempotent, low-risk)
- otherwise marks `needs-reconcile` label and causes the orchestrator to fail-closed/pause

We avoid scattering “special case fixups” throughout control code.

## Consequences
### Positive
- Correctness becomes centralized and reviewable.
- Easier to extend: add drift rules in one place.
- Easier to test: reconciliation rules can be unit-tested and replayed.

### Negative / Costs
- Requires careful definition of “safe repair” vs “pause”.
- Some initial refactoring to route all drift handling through reconcile().

## Alternatives considered
- Ad-hoc fixups in action applier / orchestrator: rejected.
- Only startup reconciliation: rejected (crash recovery matters).
- Only runtime reconciliation: rejected (runtime drift still breaks correctness).

## Follow-ups
- Define a small set of drift categories and consistent handling:
  - `WARN_ONLY`, `SAFE_REPAIR`, `REQUIRES_HUMAN`
- Add tests: startup scenarios (orphan PR, missing label) and runtime scenarios (label changed mid-tick).
