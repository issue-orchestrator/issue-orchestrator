---
name: e2e-tests
description: Run end-to-end tests against a target repository. Use when running e2e tests, debugging e2e failures, or setting up e2e test infrastructure.
---

# E2E Tests

End-to-end tests run the full orchestrator against a real GitHub repository.

## When to Use

- Running e2e tests after major changes
- Debugging e2e test failures
- Setting up e2e test infrastructure
- Verifying the full pipeline works

## Key Concept: Target Repository

E2E tests run the orchestrator against a "target repository" - this should NOT be the issue-orchestrator repo itself. The target repo is where test issues/PRs are created.

## Prerequisites Check

```bash
# 1. Check gh authentication
gh auth status

# 2. Check claude CLI
which claude

# 3. Check hook verification passes
issue-orchestrator verify
```

## Running E2E Tests

### Option 1: Use test configs (recommended)

The test configs in `tests/e2e/configs/` have proper settings:

```bash
# Run tests that use configs (timeout, rework tests)
pytest tests/e2e/test_real_scenarios.py::TestSessionTimeoutFailure -v
pytest tests/e2e/test_real_scenarios.py::TestReworkCyclesAndEscalation -v
```

### Option 2: Set E2E_TEST_REPO

```bash
# Point to a dedicated test target repo
export E2E_TEST_REPO=YourOrg/test-target-repo
pytest tests/e2e/ -v
```

### Option 3: Use test-reset command

```bash
# Reset test environment (teardown + setup)
export TEST_REPO=YourOrg/test-target-repo
issue-orchestrator test-reset

# Then run tests
export E2E_TEST_REPO=YourOrg/test-target-repo
pytest tests/e2e/ -v
```

## Key Files

| File | Purpose |
|------|---------|
| `tests/e2e/conftest.py` | E2E fixtures, `OrchestratorProcess` class |
| `tests/e2e/configs/rework-test.yaml` | Config for rework cycle tests |
| `tests/e2e/configs/timeout-test.yaml` | Config for timeout tests |
| `tests/e2e/test_real_scenarios.py` | Main e2e test scenarios |
| `scripts/setup_test_issues.py` | Creates test issues |
| `scripts/teardown_test_issues.py` | Cleans up test issues |

## Test Configs

Test configs in `tests/e2e/configs/` include:

```yaml
repo: BruceBGordon/issue-orchestrator  # Target repo
filter_label: test-data                 # Isolates test issues
dangerous:
  skip_verification: true               # Skips hook verification
```

## Common Issues

### Hook verification fails

```
STARTUP BLOCKED: Hook verification failed
```

**Fix:** Run `issue-orchestrator setup-hooks` or use a test config with `skip_verification: true`

### FileNotFoundError: issue-orchestrator

The CLI isn't in PATH. The conftest.py should use:
```python
venv_bin = Path(sys.executable).parent / "issue-orchestrator"
```

### Tests pollute main repo

Tests default to running against the main repo. Set `E2E_TEST_REPO` to a dedicated test repo.

## Two Start Functions

In `test_real_scenarios.py`:

1. **`start_orchestrator(repo)`** - Does NOT use config file, requires hook verification
2. **`start_orchestrator_with_config(config_path)`** - Uses config file with `skip_verification: true`

Most test failures happen because tests use `start_orchestrator()` without the proper config.

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `E2E_TEST_REPO` | Target repo for e2e tests | Current repo from git |
| `TEST_REPO` | Target repo for setup/teardown scripts | `BruceBGordon/issue-orchestrator` |

## Quick Diagnostic

```bash
# Check what repo tests will use
python -c "from tests.e2e.conftest import get_test_repo; print(get_test_repo())"

# List test issues in target repo
gh issue list --repo BruceBGordon/issue-orchestrator --label test-data

# Clean up test data
python scripts/teardown_test_issues.py
```
