# Integration Tests

Tests that verify wiring between components with real (not mocked) adapters.

## What These Test

- Component wiring via `entrypoints/bootstrap.py`
- Real adapter implementations (not mocks)
- Plugin registration and hook dispatch
- Claude CLI execution (if available)

## Running

```bash
pytest tests/integration/ -v
```

## Key Files

- `test_wiring.py` - Verify DI and adapter wiring
- `test_live_hooks.py` - Test hook installation/execution
- `test_claude_execution.py` - Test Claude CLI integration (requires claude)

## Difference from Unit Tests

| Unit | Integration |
|------|-------------|
| Mock all ports | Real adapters |
| Fast, isolated | Slower, real I/O |
| Test logic | Test wiring |
