---
name: troubleshoot-e2e
description: Diagnose CI workflow failures for simulated scenarios and E2E tests. Use when a GitHub Actions run fails, simulated scenario tests break, or you need to investigate test infrastructure issues in CI.
---

# Troubleshoot E2E / Simulated Scenario CI Failures

Prescriptive workflow for diagnosing CI test failures in GitHub Actions.

## When to Use

- A CI workflow run failed (red check on PR or main)
- Simulated scenario tests failing (`tests/simulated_scenarios/`)
- E2E tests failing in `validate-full` or manual runs
- "Why is CI red?" investigations
- Test infrastructure debugging (stubs, fixtures, script runners)

**For runtime session failures**, use the `session-debugging` skill instead.
**For general orchestrator issues**, use the `troubleshooting` skill instead.

---

## Step 1: Find the Failed Run

```bash
# List recent failures
gh run list --status failure --limit 10

# List all recent runs (see which are failing)
gh run list --limit 20

# Check a specific branch
gh run list --branch my-branch --limit 10

# Check main health
gh run list --branch main --limit 10 --json conclusion,createdAt,databaseId
```

---

## Step 2: Get Failure Details

```bash
# View failed run summary
gh run view <RUN_ID>

# Get ONLY the failed job logs (much faster than full logs)
gh run view <RUN_ID> --log-failed 2>&1 | tail -200

# Search for specific failure patterns
gh run view <RUN_ID> --log-failed 2>&1 | grep -A 5 "FAILED\|ERROR\|Exception"

# Get the short test summary
gh run view <RUN_ID> --log-failed 2>&1 | grep -A 20 "short test summary"
```

---

## Step 3: Identify the Failure Domain

The `validate` workflow path-filters Python-impacting changes, then runs two jobs:

| CI Job | Command | What It Covers |
|--------|---------|----------------|
| `validate-fast` | `make validate` | Typecheck, architecture lint, complexity lint, unit tests, simulated core, integration core, web tests, then VS Code tests sequentially |
| `validate-agent` | `make test-simulated-agent` and `make test-integration-agent` | Agent-backed simulated and integration slices that require real agent CLIs |

Local PR guardrails use `make validate-pr`, the cache-aware wrapper that runs the required PR suite through the internal `_validate-pr` target.

| Target | What It Runs | Test Type |
|--------|-------------|-----------|
| `typecheck` | pyright | Static analysis |
| `lint-arch` | import-linter + AST guardrails | Architecture enforcement |
| `lint-complexity` | C901/PLR0912 checks | Complexity limits |
| `test-unit` | `pytest tests/unit packages/agent_runner/tests` | Unit tests |
| `test-simulated-core` | `pytest tests/simulated_scenarios/` excluding agent-backed foreign repo cases | Simulated scenario core tests |
| `test-simulated-agent` | `$(SIMULATED_AGENT_FILES)` | Agent-backed simulated scenarios |
| `test-integration-core` | `pytest tests/integration -m "not requires_infra"` excluding agent execution files | Integration tests without external infra |
| `test-integration-agent` | `$(INTEGRATION_AGENT_FILES)` | Agent-backed integration tests |
| `test-web` | `pytest tests/web/` | Web UI tests |
| `test-vscode` | VS Code extension tests | VS Code integration, run sequentially after parallel validation |

**Note:** True E2E tests (`tests/e2e/`) only run via `make validate-full` and are NOT part of the standard `validate` workflow. They require GitHub tokens and live API access.

---

## Step 4: Diagnose by Failure Type

### 4A: Simulated Scenario Failures

These tests use a DSL-based framework in `tests/simulated_scenarios/`:

**Key infrastructure:**

| File | Purpose |
|------|---------|
| `conftest.py` | `ScriptSessionRunner`, `StubWorkingCopy`, `TempWorktreeManager`, `build_orchestrator()` |
| `scenario_dsl.py` | Fluent `Scenario` builder with `.coder()`, `.reviewer()`, `.expect_*()`, `.run()` |
| `test_simulated_scenarios.py` | 52+ test cases covering orchestrator workflows |
| `test_foreign_repo_lifecycle.py` | Real git worktree integration test |
| `fixtures/scripts/` | Bash scripts simulating agent behavior |

**How simulated tests work:**

1. `Scenario` builder configures issue, coder/reviewer commands, expectations
2. `build_orchestrator()` wires up mocks (MockGitHubAdapter, MockEventSink, etc.)
3. `ScriptSessionRunner.create_session()` runs bash scripts via `subprocess.run`
4. Orchestrator ticks until predicate met or `max_ticks` exhausted
5. Expectations (assertions) verified against captured events and state

**Common simulated scenario failure patterns:**

| Pattern | Root Cause | Fix |
|---------|-----------|-----|
| `predicate not satisfied before max_ticks` | Session never progresses past LAUNCHING | Check `ScriptSessionRunner` + stub compliance |
| `assert pr is not None` | Session failed, no PR created | Check session creation logs in CI output |
| `StubWorkingCopy has no attribute X` | Stub missing method from `WorkingCopy` protocol | Add missing method to `StubWorkingCopy` |
| Script exit code != 0 | Fixture bash script failed | Check script, Python path, permissions |
| `make install-vscode-extensions` warning | CI doesn't have that target | Non-fatal warning, not root cause |

### 4B: StubWorkingCopy Protocol Drift

The `StubWorkingCopy` class in `tests/simulated_scenarios/conftest.py` must implement all methods from the `WorkingCopy` protocol (`src/issue_orchestrator/ports/working_copy.py`).

When new methods are added to the protocol, the stub may fall behind. The `detect_existing_work()` function in `session_launcher.py` catches `AttributeError` gracefully, but other callers may not.

**To check for drift:**

```bash
# Methods in the protocol
grep "def " src/issue_orchestrator/ports/working_copy.py | grep -v "class\|#"

# Methods in the stub
grep "def " tests/simulated_scenarios/conftest.py | grep -v "class\|#"
```

### 4C: ScriptSessionRunner Python Resolution

The `ScriptSessionRunner` prepends the venv's Python bin dir to PATH (conftest.py). If fixture scripts call `python` and this path resolution fails, sessions will always return exit code != 0.

**Symptoms:** Every session transitions `AVAILABLE -> LAUNCHING -> FAILED` on every tick.

**Check:**

```bash
# Verify the Python resolution logic in ScriptSessionRunner
grep -A 5 "python_bin_dir\|sys.executable" tests/simulated_scenarios/conftest.py
```

### 4D: Unit / Integration / Web Test Failures

```bash
# Run locally to reproduce
make test-unit
make test-integration-core
make test-web

# Agent-backed slices from the validate-agent CI job
make test-simulated-agent
make test-integration-agent

# Run specific failing test
pytest tests/unit/test_foo.py::test_bar -v

# Stress test for flaky tests (100x under CPU load)
make -f repo-specific/Makefile stress-test TEST=tests/unit/test_foo.py::test_bar
```

### 4E: Type Check / Lint Failures

```bash
make typecheck        # pyright
make lint-arch        # import-linter + AST guardrails
make lint-complexity  # C901/PLR0912 checks
```

---

## Step 5: Reproduce Locally

```bash
# Fast validation job
make validate

# Full PR validation gate, matching standard pre-push coverage
make validate-pr

# Simulated scenario slices
make test-simulated-core
make test-simulated-agent

# Integration slices
make test-integration-core
make test-integration-agent

# Full validation, including live E2E
make validate-full

# Specific simulated test
pytest tests/simulated_scenarios/test_simulated_scenarios.py::test_name -v

# With verbose orchestrator logging
pytest tests/simulated_scenarios/ -v --log-cli-level=DEBUG
```

---

## Step 6: Check if Already Fixed

Before spending time debugging, check if the failure is on a stale branch:

```bash
# Is main green?
gh run list --branch main --limit 5

# Was the PR merged? (failure may be from dev iteration)
gh pr view <PR_NUMBER> --json state,mergedAt

# Has the file been changed since the failure?
git log --oneline --since="<failure-date>" -- <failing-file>
```

---

## Historical Failure Patterns

### Feb 2026: Simulated Scenario Python Resolution (PR #4065)

**Symptom:** `test_local_loop_two_rounds_of_review` and `test_local_loop_happy_path_creates_non_draft_pr` failing. Sessions stuck in `LAUNCHING -> FAILED` loop.

**Root cause:** `ScriptSessionRunner.create_session()` ran fixture bash scripts that called `python`, but the venv Python wasn't on PATH in CI. Scripts failed with nonzero exit, causing every session to fail.

**Fix:** Prepend `sys.executable`'s parent directory to `PATH` in `ScriptSessionRunner.create_session()`.

**Concurrent issue:** `StubWorkingCopy` missing `get_commits_ahead_of_main` method (caught by try/except, warning-only, not the primary failure cause).

---

## Key File Reference

| File | Purpose |
|------|---------|
| `.github/workflows/validate.yml` | CI workflow definition |
| `Makefile` (`validate`, `validate-pr`, `validate-full`, `_validate-impl`, `_validate-pr-impl`) | Local and CI validation target graph |
| `tests/simulated_scenarios/conftest.py` | Simulated test infrastructure |
| `tests/simulated_scenarios/scenario_dsl.py` | Test DSL framework |
| `tests/simulated_scenarios/fixtures/scripts/` | Agent simulation bash scripts |
| `src/issue_orchestrator/ports/working_copy.py` | WorkingCopy protocol (source of truth) |
| `src/issue_orchestrator/control/session_launcher.py` | Session launch logic |
| `src/issue_orchestrator/control/session_manager.py` | Session state management |
