# Testing Guide

## Running Tests

```bash
source .venv/bin/activate
pytest tests/unit/              # Run unit tests
pytest tests/unit/ -v           # Verbose
pytest tests/unit/ --cov        # With coverage (90%+)
pytest tests/integration/       # Integration tests (requires Claude CLI)
E2E_TEST_REPO=youruser/your-test-repo make test-e2e   # Live e2e tests (see E2E Tests)
```

## Test Structure

```
tests/
├── unit/                 # Fast, isolated tests
├── integration/          # Tests with real external systems
├── e2e/                  # Full pipeline tests with GitHub
├── conftest.py           # Shared fixtures
└── fixtures/             # Test data files
```

## Key Fixtures (conftest.py)

### Mock Adapters

```python
@pytest.fixture
def mock_github_adapter():
    """Mock GitHub adapter implementing port interfaces."""
    adapter = MockGitHubAdapter()
    adapter.issues = [Issue(...), ...]  # Set up test data
    return adapter

@pytest.fixture
def mock_terminal_plugin():
    """Mock terminal plugin for session operations."""
    plugin = MockTerminalPlugin()
    plugin.session_exists_override = False  # Control behavior
    return plugin
```

### Mock Ports

```python
class MockEventSink:
    """Collects events for test assertions."""
    def __init__(self):
        self.events: list = []

    def publish(self, event) -> None:
        self.events.append(event)

class MockSessionRunner:
    """Tracks session operations."""
    def __init__(self, plugin: MockTerminalPlugin):
        self._plugin = plugin

    def create_session(self, ...): ...
    def session_exists(self, ...): ...
```

### Auto-Patching

The `patch_orchestrator_dependencies` fixture auto-injects mocks into all Orchestrator instances:

```python
@pytest.fixture(autouse=True)
def patch_orchestrator_dependencies(monkeypatch, request):
    """Auto-patch orchestrator dependencies for all tests."""
    # Patches __post_init__ to inject MockEventSink and MockSessionRunner
    # Returns {'events': mock_events, 'runner': mock_runner}
```

## Testing Patterns

### Test at Port Boundaries

```python
# GOOD: Mock the port, test orchestrator logic
def test_launch_session(sample_orchestrator, mock_github_adapter):
    mock_github_adapter.issues = [Issue(number=42, ...)]
    await sample_orchestrator.launch_session(issue)
    assert mock_github_adapter.add_label_calls == [(42, "in-progress")]

# BAD: Patch internal functions
def test_launch_session():
    with patch('issue_orchestrator.github._run_gh'):  # Don't do this
        ...
```

### Use Fixtures for Sample Data

```python
@pytest.fixture
def sample_issues():
    return [
        Issue(number=1, title="High priority", labels=["priority:high"]),
        Issue(number=2, title="Medium priority", labels=["priority:medium"]),
    ]
```

### Test CLI Commands

```python
def test_cmd_start(self):
    with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
        with patch('issue_orchestrator.entrypoints.bootstrap.build_orchestrator') as mock_build:
            mock_config = Mock()
            mock_config.validate.return_value = []  # Pass validation
            mock_find.return_value = mock_config
            mock_build.return_value = Mock()

            result = cmd_start(args)
            assert result == 0
```

## E2E Tests

Live tests that create, edit and close real GitHub issues and PRs. Because of
that, each of three safeguards fails closed on its own (#9523):

- **Opt-in only.** `tests/e2e` is collected only with `IO_E2E=1`. A bare
  `pytest`, an empty path list or `pytest tests/` skips it, and naming a path
  inside it without the opt-in is a usage error. The suite's own conftest
  refuses too, so a run that skips the root conftest (`--confcutdir`) is still
  refused. Inside an orchestrator agent session
  (`ISSUE_ORCHESTRATOR_SESSION_ID` set) the opt-in never applies.
- **Never the production repository.** `E2E_TEST_REPO` is required and must
  not be this checkout's own `origin` repository. There is no git-remote
  fallback.
- **Cleanup removes only what a scope proves is this run's.** Every PR close,
  branch delete, issue close and label delete goes through one owner
  (`tests/e2e/fixtures/artifact_cleanup.py`). A run closes only the issues it
  recorded creating (or that carry a label it minted with its own random id)
  and their PRs carrying an e2e label, and deletes no label. A branch goes
  only as an admitted PR's head, or with a receipt proving the run created it
  (absent when the run started, holding the run's commit, still its head) --
  never by name, and only when every open PR it heads or is the base of (read
  completely, uncached) is admitted too. No run can prove another has ended,
  so other runs' open e2e PRs and issues are only reported; an operator who
  knows no run is live removes them, and the e2e labels, explicitly:
  `IO_E2E=1 E2E_TEST_REPO=<repo> python -m tests.e2e.reconcile
  --no-live-run-against <repo>`. Workflow labels such as `needs-code-review`
  never put anything in scope.

```bash
# Run e2e tests (make sets IO_E2E=1; requires gh auth + write access to the test repo)
E2E_TEST_REPO=youruser/your-test-repo make test-e2e

# Run one e2e file directly
IO_E2E=1 E2E_TEST_REPO=youruser/your-test-repo pytest tests/e2e/test_live_orchestrator.py -v

# Run e2e tests with subprocess terminal backend
IO_E2E=1 E2E_TEST_REPO=youruser/your-test-repo E2E_TERMINAL_ADAPTER=subprocess pytest tests/e2e/test_terminal_adapter_subprocess.py -v
```

**Prerequisites:**
- `gh` CLI authenticated with write access to the test repository
- `claude` CLI available
- Network access to GitHub

**Test repository:**
1. Create a dedicated test repository (or a fork you do not develop in)
2. Set: `E2E_TEST_REPO=youruser/yourrepo`
3. Run e2e tests against it

## Native Stack CI Coverage

GitHub evaluates every pull request in a native stack against the stack trunk.
Running the aggregate `validate-fast` job on every layer therefore repeats the
same cumulative work. When the existing changed-path filter selects this job,
the workflow runs it fully for standalone pull requests, the cumulative top
layer, the current lowest unmerged layer, every merge-group candidate, and
pushes to `main`. Intermediate stack layers keep the required check name but
defer the expensive setup and test steps.

This repository requires GitHub Merge Queue for `main`. Every merge-group
candidate runs the full job, including a contiguous group that ends at an
intermediate stack layer. If layers land serially, GitHub rebases the next layer
onto the trunk and makes it the lowest unmerged pull request; that new exact head
also runs the full pull-request job. Missing or malformed stack metadata runs the
full job conservatively. The selection policy is owned by
`scripts/stack_ci_validation.py`.

## Test Mode

Run orchestrator with mock data:
```bash
issue-orchestrator start --test-mode
```
