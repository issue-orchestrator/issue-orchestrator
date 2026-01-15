# E2E Tests

Live tests that create real GitHub issues and run the full orchestrator.

## Prerequisites

- GitHub token configured with write access
- `claude` CLI available
- Network access to GitHub

## Running

```bash
pytest tests/e2e/ -v                    # Run all e2e tests
pytest tests/e2e/ -k "test_name" -v     # Specific test
pytest tests/ --ignore=tests/e2e/       # Skip e2e tests

# Run terminal adapter e2e with subprocess backend
E2E_TERMINAL_ADAPTER=subprocess pytest tests/e2e/test_terminal_adapter.py -v
```

## Test Isolation

Tests use `io-e2e-test-data` label to isolate test artifacts:
- Issues and PRs created by e2e tests get this label
- Cleanup only targets items with this explicit label
- No branch pattern matching (avoids accidentally deleting legitimate PRs)

## E2E Reconciliation

Before running tests, the `e2e_reconciliation_at_session_start` fixture cleans up artifacts from previous runs:

1. Local worktrees in `/tmp/e2e-worktrees/`
2. Stale tmux sessions
3. PRs and branches with `io-e2e-test-data` label
4. Issues with `io-e2e-test-data` label

This ensures deterministic test runs regardless of previous state.

## Key Files

- `conftest.py` - E2E fixtures (e2e_config, ipc_client, orchestrator_process)
- `test_data.py` - Create/cleanup test issues on GitHub

## Coding Style

- Use `logging` module, not `print()` - allows proper log filtering
- Use `logger.info()` for status messages, `logger.warning()` for issues
- Prefer event-stream waits and targeted refreshes over full refreshes.
  Only trigger a full refresh when there is no inflight ID to target.

## For Contributors (no write access)

1. Fork repo or create test repo
2. Set `E2E_TEST_REPO=youruser/yourrepo`
3. Run e2e tests against your repo

## What E2E Tests Verify

- Issue creation and pickup
- Session launch and completion
- PR creation via agent-done
- Label management
- Event observation via IPC
