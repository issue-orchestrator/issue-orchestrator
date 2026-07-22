# ADR 0007: External State Reconciliation

**Status:** Accepted
**Date:** 2024-12-22

## Context

GitHub (issues, labels, PRs) is the authoritative external state for Issue Orchestrator. The orchestrator makes decisions based on snapshots of this state, but the state can change between observation and mutation due to:
- Human intervention (manual label changes, PR merges)
- Other tools or automation
- Race conditions in concurrent operations

Without verification, the orchestrator could apply stale transitions, overwrite human changes, or create inconsistent state.

## Decision

**Verify external state matches expected prior state before any mutation.**

This is optimistic concurrency control applied to GitHub:

1. **Before any mutation** (labels, PR creation/closure, comments implying state):
   - Fetch current external snapshot
   - Compare snapshot against expected prior state used to compute the transition

2. **On match**: Apply mutations via adapters, optionally re-fetch to confirm

3. **On mismatch**: Abort transition and enter reconciliation flow

## Consequences

### Positive
- Race conditions with humans or other tools are detected, not silently overwritten
- Stale transitions are caught before causing drift
- Partial or contradictory state updates are prevented
- System fails safe (pause) rather than fail dangerous (overwrite)

### Negative
- Additional API calls for verification (mitigated by caching/ETags per ADR-0006)
- Reconciliation flow adds complexity
- Some operations may be delayed while awaiting reconciliation

## Failure Handling

On mismatch, the system MUST NOT:
- Partially apply mutations
- Guess intent
- Overwrite external state

Valid responses:
- Pause issue/session
- Mark `needs-reconciliation`
- Notify human or tech lead agent

## Related

- ADR-0002: Write-then-observe pattern (verify writes succeeded)
- ADR-0004: Centralize reconciliation
- ADR-0006: Caching with ETags (efficient re-fetching)
