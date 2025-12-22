# Agent Implementation Tasks: Reconciliation

This checklist is authoritative for implementing reconciliation.

## 1. Define external snapshot model
- [ ] Create `ExternalSnapshot` capturing:
  - issue labels
  - PR state (open/closed/merged)
  - review state if applicable
Acceptance:
- Snapshot can be compared for equality/subset checks.

## 2. Define expected state on transitions
- [ ] Extend transition definitions to include expected prior state
Acceptance:
- Each transition declares what must be true externally.

## 3. Implement reconcile/apply API
- [ ] Implement single function:
      `apply_transition(expected_state, mutations)`
Acceptance:
- All external writes go through this function.
- Function fetches fresh snapshot before mutation.

## 4. Abort on mismatch
- [ ] If snapshot does not satisfy expected_state:
  - raise `ReconciliationRequired`
  - do not mutate
Acceptance:
- No adapter mutation occurs on mismatch.

## 5. Integrate into orchestrator flow
- [ ] Call reconciliation before:
  - label updates
  - PR creation/closure
  - terminal state comments
Acceptance:
- No direct adapter writes remain outside reconciliation.

## 6. Surface reconciliation failures
- [ ] Emit event or status indicating reconciliation required
Acceptance:
- Human/triage agent can detect and respond.
