# Testing Principles

## Test Behavior, Not Implementation

**The cardinal rule**: Tests should verify WHAT the code does, not HOW it does it.

When you test implementation details (private methods, internal state), you:
- Couple tests to code structure, so refactoring breaks tests
- Miss actual bugs because you're testing the wrong thing
- Create maintenance burden as implementation evolves

### The Litmus Test

Before writing a test, ask: **"Would a user of this code care about this?"**

- If yes → test it through the public API
- If no → don't test it directly (it's an implementation detail)

---

## Never Access Private Members (`_xxx`)

**Do not access `_private` members in tests.** This is enforced by ruff's SLF001 rule.

If you find yourself needing to access a private member, stop and ask why:

| Smell | What It Means | Fix |
|-------|---------------|-----|
| `obj._method()` | Testing implementation | Test through public API instead |
| `obj._cache.set(...)` | Missing dependency injection | Inject the collaborator |
| `assert obj._state == X` | Testing internal state | Test observable outcome |
| `obj._flag = False` | Missing configuration surface | Make it a constructor param |

### Example: Testing Cache Behavior

```python
# BAD - Reaches into internals
def test_cache_stores_labels(self, adapter):
    adapter.get_issue_labels(42)
    cached = adapter._cache.get_issue_labels(42)  # SLF001 violation!
    assert cached == ["bug"]

# GOOD - Tests observable behavior
def test_subsequent_calls_use_cache(self, adapter, mock_http):
    adapter.get_issue_labels(42)  # First call hits API
    adapter.get_issue_labels(42)  # Second call should use cache
    assert mock_http.get_issue_labels.call_count == 1  # Only one API call
```

### Example: Testing Helper Methods

```python
# BAD - Tests private helper directly
def test_build_labels(self, orchestrator):
    labels = orchestrator._build_labels("agent:web")  # SLF001 violation!
    assert labels == ["agent:web", "test-data"]

# GOOD - Tests through public behavior
def test_session_labels_include_filter(self, orchestrator, mock_github):
    orchestrator.start_session(issue)
    # Verify the labels that were actually applied
    call_args = mock_github.add_labels.call_args
    assert "test-data" in call_args[0][1]
```

---

## Dependency Injection Over Internal Access

If a test needs to control or observe a collaborator, that collaborator should be **injected**, not accessed internally.

```python
# BAD - Test manipulates internal collaborator
def test_with_cache_disabled(self, adapter):
    adapter._cache_enabled = False  # Reaching into internals
    ...

# GOOD - Collaborator is injected
def test_with_cache_disabled(self):
    adapter = GitHubAdapter(cache_enabled=False)  # Configuration at construction
    ...

# BETTER - Cache is a separate injectable dependency
def test_cache_behavior(self):
    cache = InMemoryCache()
    cache.set_issue_labels(42, ["bug"])
    adapter = GitHubAdapter(cache=cache)  # Inject the collaborator
    ...
```

---

## What to Test

### DO Test
- **Public method behavior** - Given inputs, verify outputs/effects
- **Error handling** - Given bad inputs, verify appropriate errors
- **State transitions** - Given action, verify observable state change
- **Integration points** - Verify collaborators are called correctly (via mocks)

### DON'T Test
- **Private methods directly** - They're implementation details
- **Internal state** - Test observable outcomes instead
- **How something works** - Test what it does

---

## Mock at Port Boundaries

This codebase uses hexagonal architecture. Mock at the **port** level, not inside implementations.

```python
# BAD - Patching deep internals
with patch('issue_orchestrator.execution.github_http.GitHubHttpClient'):
    ...

# GOOD - Mock the port interface
mock_github_adapter.get_issue_labels.return_value = ["bug"]
orchestrator = create_orchestrator(github=mock_github_adapter)
```

See `tests/unit/conftest.py` for pre-built mock adapters.

---

## When You Can't Test Without Private Access

If you genuinely can't verify behavior without accessing internals, this signals a design issue:

1. **Extract a collaborator** - The private thing should be its own class with a public interface
2. **Add observability** - The class needs a way to report its state publicly
3. **Question the test** - Maybe this behavior doesn't need explicit testing

**Never "fix" this by making `_private` → `public`.** The author marked it private for a reason. Fix the design, not the visibility.

---

## Subdirectory Guides

- `unit/AGENTS.md` - Unit test patterns, fixtures, mocking
- `integration/AGENTS.md` - Integration test patterns
- `e2e/AGENTS.md` - E2E test setup, GitHub requirements
