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
   `E2E_TEST_REPO` is required and must not be this checkout's `origin`,
   nor the `origin` of any checkout in the io repo registry
   (`~/.config/issue-orchestrator/repos.json`): a repository an io engine
   manages is never a test repository (#9529). A registered checkout that is
   missing or has no GitHub origin refuses every target. `OrchestratorProcess`
   points each engine it starts at a registry of its own, so the suite's
   engines (some run from clones of the test repository) never land there.
3. **One cleanup owner** (`fixtures/artifact_cleanup.E2EArtifactCleanup`):
   every PR close, branch delete, issue close and label delete goes through
   it, and it admits only what a typed scope proves. `RunOwnedScope`
   (`E2EFlow`, and the session's teardown over `RUN_ISSUES`): the issues the
   run recorded creating, and their PRs carrying an e2e label.
   `RunLabelScope`: issues carrying a label the run minted with its own random
   id (engine-filed tech-lead issues). `ExamRunScope` (the exam): its owned
   issues, PRs whose body links one, and branches it holds a
   `RemoteBranchReceipt` for (absent when the run started, holding the run's
   commit, still its head; `exam/run_branches.py`). `IdleRepositoryScope`:
   only an operator's explicit `python -m tests.e2e.reconcile`. A branch is
   never deleted by name, nor while an open PR it heads or bases (read
   completely, uncached) is outside the scope; no label is deleted
   automatically. Create issues with `create_run_issue` (or an `E2EFlow`),
   close them through the owner (`flow.close_issue`, `close_run_issue`), never
   call the adapter's `close_pr`/`delete_branch`/`update_issue_state`/
   `delete_label` from a test, and never clean up by a workflow label
   (`needs-code-review`, `code-reviewed`, ...): real work carries those.

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
3. Open PRs and issues carrying `io-e2e-test-data` that this run did not
   create are only *reported*: nothing a run can observe proves the run that
   made them has ended (a long suite, a delayed dependency or a stuck process
   outlives any fixed age), so they are never closed automatically, and no
   label is deleted

After the session, every issue this run recorded creating (`RUN_ISSUES`) is
closed, with its e2e PRs and their branches.

To remove other runs' leftover PRs, issues and e2e labels, first make sure no
run is live against the test repository, then say so explicitly:

```bash
IO_E2E=1 E2E_TEST_REPO=owner/test-repo \
    python -m tests.e2e.reconcile --no-live-run-against owner/test-repo
```

It removes PRs (with their branches) and issues before labels, and deletes a
label only once complete reads prove nothing still depends on it -- no open
issue or PR, and no closed PR whose branch survives: an artifact the engine
filed may carry nothing but its run's label. If anything fails or a listing
cannot be proven complete, it keeps every label, lists what is unfinished and
exits non-zero; run it again once the cause is fixed.

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
