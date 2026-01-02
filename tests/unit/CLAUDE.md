# Unit Tests

## Patterns

1. **Mock at port boundaries** - Use MockGitHubAdapter, MockEventSink, not internal patches
2. **Auto-patching** - `patch_orchestrator_dependencies` fixture injects mocks into all Orchestrator instances
3. **Use fixtures** - Leverage `conftest.py` for sample data

## Key Fixtures (conftest.py)

```python
# Auto-injected (autouse=True)
patch_orchestrator_dependencies  # Injects MockEventSink, MockSessionRunner

# Sample data
sample_issues, sample_config, sample_orchestrator

# Mock adapters
mock_github_adapter, mock_terminal_plugin, mock_event_sink
```

## Running

```bash
pytest tests/unit/ -v              # All
pytest tests/unit/test_X.py -v     # One file
pytest tests/unit/ -k "test_name"  # By name
pytest tests/unit/ --cov           # With coverage
```

## Anti-patterns

```python
# BAD - patching internals
with patch('issue_orchestrator.adapters.github.http_client.GitHubHttpClient'): ...

# GOOD - mock the port
mock_github_adapter.issues = [Issue(...)]
```
