# Reconciliation Tests

## Runtime (always-on)
- Re-fetch external snapshot immediately before mutation
- Compare against expected state
- Abort on mismatch

## Unit Tests
- snapshot comparison logic
- expected_state satisfaction checks
- reconciliation abort path raises correct exception

## Integration Tests
- simulate label changed externally between plan and apply
- verify no mutation occurs
- verify reconciliation signal emitted

## End-to-End Tests
- human changes label mid-run
- orchestrator attempts transition
- system pauses and requires reconciliation

## Negative Tests
- ensure no adapter method can mutate state without reconciliation
