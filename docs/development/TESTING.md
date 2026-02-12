# Testing Guide

## Running Tests

```bash
source .venv/bin/activate
pytest tests/unit/              # Run unit tests (~4000 tests)
pytest tests/unit/ -v           # Verbose
pytest tests/unit/ --cov        # With coverage (90%+)
pytest tests/integration/       # Integration tests (requires Claude CLI)
pytest tests/e2e/ -v            # Live e2e tests (requires gh auth)
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
        with patch('issue_orchestrator.bootstrap.build_orchestrator') as mock_build:
            mock_config = Mock()
            mock_config.validate.return_value = []  # Pass validation
            mock_find.return_value = mock_config
            mock_build.return_value = Mock()

            result = cmd_start(args)
            assert result == 0
```

## E2E Tests

Live tests that create real GitHub issues:

```bash
# Run e2e tests (requires gh auth + write access)
pytest tests/e2e/ -v

# Run e2e tests with subprocess terminal backend
E2E_TERMINAL_ADAPTER=subprocess pytest tests/e2e/test_terminal_adapter_subprocess.py -v

# Skip e2e tests
pytest tests/ --ignore=tests/e2e/
```

**Prerequisites:**
- `gh` CLI authenticated with write access
- `claude` CLI available
- Network access to GitHub

**For contributors (no write access):**
1. Fork the repo or create your own test repo
2. Set: `E2E_TEST_REPO=youruser/yourrepo`
3. Run e2e tests against your repo

## Test Mode

Run orchestrator with mock data:
```bash
issue-orchestrator start --test-mode
```
