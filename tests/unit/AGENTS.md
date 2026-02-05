# Unit Tests

## Determinism Required

**Deterministic tests only.** Any timing-based coordination is forbidden.

- Do **not** use `sleep` (including `asyncio.sleep`) to “wait for” work.
- Do **not** rely on “eventually” semantics, background timing, or real clocks.
- Do use deterministic control points: explicit callbacks, single-tick helpers, mocked time, or injected schedulers.
- If a test can flake under load or on CI, it’s wrong—fix the test, not the timeout.
- Exception: tests that must wait on **real external systems** may use bounded waits, but should still prefer explicit readiness/ack signals over sleeps.
- Mocked or inert sleeps used purely to **simulate time** (without waiting) are acceptable.

## Patterns

1. **Mock at port boundaries** - Use MockGitHubAdapter, MockEventSink, not internal patches
2. **Auto-patching** - `patch_orchestrator_dependencies` fixture injects mocks into all Orchestrator instances
3. **Use fixtures** - Leverage `conftest.py` for sample data
4. **Thread coordination helpers** - Use `tests/unit/threading_helpers.py` instead of sleeps

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
