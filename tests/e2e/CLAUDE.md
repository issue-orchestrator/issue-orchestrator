# E2E Tests

Live tests that create real GitHub issues and run the full orchestrator.

## Prerequisites

- `gh` CLI authenticated with write access
- `claude` CLI available
- Network access to GitHub

## Running

```bash
pytest tests/e2e/ -v                    # Run all e2e tests
pytest tests/e2e/ -k "test_name" -v     # Specific test
pytest tests/ --ignore=tests/e2e/       # Skip e2e tests
```

## Test Isolation

Tests use `test-data` label to isolate test issues:
- `create_test_issues()` creates issues with this label
- `cleanup_test_issues()` closes all issues with this label
- Filter with `--filter-label test-data`

## E2E Reconciliation

Before running tests, the `e2e_reconciliation_at_session_start` fixture cleans up ALL artifacts from previous (possibly crashed) runs:

1. Local worktrees in `/tmp/e2e-worktrees/`
2. Stale tmux sessions
3. Remote branches matching e2e patterns
4. Open PRs with test labels or e2e branch patterns
5. Open issues with `test-data` label

This ensures deterministic test runs regardless of previous state.

## Key Files

- `conftest.py` - E2E fixtures (e2e_config, ipc_client, orchestrator_process)
- `test_data.py` - Create/cleanup test issues on GitHub

## Coding Style

- Use `logging` module, not `print()` - allows proper log filtering
- Use `logger.info()` for status messages, `logger.warning()` for issues

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
