# E2E Tests

Live tests that create, edit and close real GitHub issues and PRs and run the
full orchestrator.

## Never From An Agent Session

On 2026-10-14 (#9523) an agent's bare `pytest` fell back to `testpaths`, ran
this suite against the production repository, and its cleanup closed 17 real
PRs. Three safeguards now fail closed independently:

1. **Opt-in only** (`tests/live_e2e_gate.py`): collected only with `IO_E2E=1`.
   Implicit runs skip it; naming it without the opt-in is a usage error. This
   suite's own conftest refuses as well, so excluding the root conftest
   (`--confcutdir`) does not open it. Inside an agent session
   (`ISSUE_ORCHESTRATOR_SESSION_ID` set) the opt-in never applies. Agents: do
   not run this suite, and do not try to opt in.
2. **Never production** (`fixtures/github_utils.get_test_repo`):
   `E2E_TEST_REPO` is required and must not be this checkout's `origin`.
3. **One cleanup owner** (`fixtures/artifact_cleanup.E2EArtifactCleanup`):
   every PR close and branch delete goes through it, and it admits only what
   a typed scope proves. `RunOwnedScope` (a run's teardown, `E2EFlow`): an
   e2e label on the PR and an issue the run created. `StaleArtifactScope`
   (session reconciliation): `io-e2e-test-data` and a creation time older
   than any live run, so a concurrent run's PRs are never touched.
   `ExamRunScope` (the exam): the run's own issues and pushed branches. A
   branch goes only with an admitted PR, never by name. Never call the
   adapter's `close_pr`/`delete_branch` from a test, and never clean up by a
   workflow label (`needs-code-review`, `code-reviewed`, ...): real work
   carries those.

## Prerequisites

- `IO_E2E=1` (the `make` targets set it) and `E2E_TEST_REPO=<owner>/<test-repo>`
- GitHub token configured with write access to the test repository
- `claude` CLI available
- Network access to GitHub

## Running

```bash
make test-e2e                                        # Run all e2e tests
make test-e2e-one TEST=test_name                     # Specific test
IO_E2E=1 pytest tests/e2e/test_live_orchestrator.py -v   # One file directly

# Run terminal adapter e2e with subprocess backend
IO_E2E=1 E2E_TERMINAL_ADAPTER=subprocess pytest tests/e2e/test_terminal_adapter_subprocess.py -v

# Run VS Code extension e2e (requires VS Code deps installed)
IO_E2E=1 pytest tests/e2e/test_vscode_integration.py -v
```

All of these also need `E2E_TEST_REPO` set.

## Test Isolation

Tests use `io-e2e-test-data` label to isolate test artifacts:
- Issues and PRs created by e2e tests get this label
- A test closes a PR through the `E2EFlow` that created its issue
  (`flow.close_pr`), so the flow's scope owns it
- No branch pattern matching (avoids accidentally deleting legitimate PRs)

## E2E Reconciliation

Before running tests, the `e2e_reconciliation_at_session_start` fixture cleans up artifacts from previous runs:

1. Local worktrees in `/tmp/e2e-worktrees/`
2. Stale tmux sessions
3. PRs carrying `io-e2e-test-data` older than any live run
   (`MAX_LIVE_RUN_AGE`), and their branches; younger ones belong to a run
   that may still be going, whose own teardown removes them
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

## Test Repository

1. Create a dedicated test repository (or a fork you do not develop in)
2. Set `E2E_TEST_REPO=youruser/yourrepo`
3. Run e2e tests against it

## What E2E Tests Verify

- Issue creation and pickup
- Session launch and completion
- PR creation via coding-done
- Label management
- Event observation via IPC
