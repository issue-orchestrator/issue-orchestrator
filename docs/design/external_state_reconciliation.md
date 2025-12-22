# External State Reconciliation

**Audience:** Design document (public). Not a usage guide.

## Invariant: External State Must Match Expected Prior State

GitHub (issues, labels, PRs) is the authoritative external state for Issue Orchestrator.
Before mutating any external state, the orchestrator MUST re-fetch the current external
state and verify that it matches the expected prior state used to compute the transition.

This invariant prevents:
- race conditions with humans or other tools
- stale transitions
- partial or contradictory state updates

## Rule

Before any mutation (labels, PR creation/closure, comments implying state):
1. Fetch current external snapshot.
2. Compare snapshot against expected prior state.
3. If mismatch:
   - Abort transition.
   - Enter reconciliation flow.
4. If match:
   - Apply mutations via adapters.
   - Optionally re-fetch to confirm.

This is optimistic concurrency control applied to GitHub.

## Failure Handling

On mismatch, the system MUST NOT:
- partially apply mutations
- guess intent
- overwrite external state

Valid responses:
- pause issue/session
- mark `needs-reconciliation`
- notify human or triage agent
