---
name: refactoring
description: Safe refactoring practices for reducing code complexity. Use when addressing C901/PLR0912 violations, extracting functions, or restructuring code while preserving behavior.
---

# Safe Refactoring

Guidelines for refactoring code to reduce complexity (C901, PLR0912) without introducing behavioral changes.

## When to Use

- Addressing Ruff C901 (function too complex) violations
- Addressing Ruff PLR0912 (too many branches) violations
- Extracting helper functions from large functions
- Restructuring code while preserving exact behavior

## The Two Golden Rules

### 1. Refactoring changes structure, not behavior

If tests pass before and after but behavior changed, your tests have gaps.

### 2. Extract meaningful phases, not arbitrary splits

Helpers should represent **real phases a human would describe**, not arbitrary code chunks that happen to reduce measured complexity.

---

## Meaningful Extraction vs Metric-Driven Splitting

### The Wrong Approach: Splitting to Satisfy Metrics

```python
# BAD - Arbitrary extraction just to reduce complexity
def _validate_part_1(self, issue):
    if issue.agent_type is None:
        return LaunchResult(None, False, "no agent type")
    return None

def _validate_part_2(self, issue):
    if not self.config.agents.get(issue.agent_type):
        return LaunchResult(None, False, "no config")
    return None

def launch(self, issue):
    if result := self._validate_part_1(issue):
        return result
    if result := self._validate_part_2(issue):
        return result
    # ... more arbitrary splits
```

Problems:
- Helper names describe *mechanics* ("part_1") not *purpose*
- `Optional` and `None` returns proliferate
- The coordinator becomes a chain of "if result" checks
- A reader can't understand the function from its outline

### The Right Approach: Phase-Based Extraction

First, identify the **distinct phases** a human would describe:

| Phase | Purpose |
|-------|---------|
| Validate preconditions | Check config, conflicts, permissions |
| Verify dependencies | CAS check that dependencies still satisfied |
| Acquire resources | Claims, locks, reservations |
| Prepare environment | Worktrees, directories, metadata |
| Execute action | The actual work |
| Report outcome | Events, state transitions |

Then extract helpers that map to these phases:

```python
# GOOD - Phases a human would describe
def launch_issue_session(self, issue, active_sessions) -> LaunchResult:
    # Validate prerequisites
    if result := self._check_launch_preconditions(issue, active_sessions):
        return result

    # Verify dependencies haven't changed since scheduling
    if result := self._verify_dependencies_fresh(issue):
        return result

    # Acquire distributed claim
    claim = self._acquire_claim_for_issue(issue)
    if not claim.success:
        return claim.as_result()

    # Prepare worktree and session environment
    ctx = self._prepare_session_environment(issue, session_name, ...)
    if ctx.error:
        self._release_claim(issue, claim)
        return LaunchResult(None, False, f"Worktree failed: {ctx.error}")

    # Launch the terminal session
    return self._start_session_terminal(issue, ctx, claim)
```

Benefits:
- The coordinator reads like a **table of contents**
- Each helper has a **meaningful name** describing a phase
- Error handling remains **visible** at the top level
- Domain-relevant types, not `Optional` proliferation

### When to Use Strategic `noqa`

Some functions are **inherently complex coordinators**. If extraction would:
- Create meaningless helpers (e.g., `_handle_step_3`)
- Scatter related error handling across multiple functions
- Make the code harder to understand despite lower metrics

Then consider strategic `noqa`:

```python
def orchestrate_complex_workflow(self, ...):  # noqa: C901, PLR0912
    """Coordinates X, Y, and Z phases.

    Complexity is inherent - this is a multi-step coordinator with
    distinct failure modes at each step. Extracting arbitrary helpers
    would obscure the flow.
    """
    # Phase 1: ...
    # Phase 2: ...
```

**Use `noqa` when:**
- The complexity is inherent to the coordination, not incidental
- Extraction would create helpers that just split code, not represent phases
- The function is already well-organized with clear phase comments
- Tests adequately cover the behavior

**Don't use `noqa` when:**
- You can identify 3+ meaningful phases worth naming
- Error handling is scattered and confusing
- The function mixes multiple concerns that should be separate

### The Litmus Test

Before extracting a helper, ask:

> "If I were explaining this function to a colleague, would I naturally describe this as a distinct step?"

- **Yes** → Extract it as a named phase
- **No** → Keep it inline or consider `noqa`

---

## Before You Start

### 1. Map All Data Flows

Before extracting any code, trace:
- What data enters the code block?
- What data is transformed or created?
- What data exits (return values, mutations, side effects)?
- What data flows through on **failure paths**?

```python
# Example: This code has data flow on the failure path
def original():
    result = try_something()
    if not result.success:
        reason = result.failure_reason  # <-- Data flows even on failure!
        cleanup(reason)
        # ... later uses reason for reporting
```

### 2. Identify All Return Semantics

Functions don't just return "success or nothing". Common patterns:
- Success with data
- Failure with reason (for logging, reporting, retry decisions)
- Failure with cleanup context
- Partial success with warnings

**The bug that prompted this skill:** Helper functions were returning `None` on failure, but the original code returned failure *with a reason* that was used later for `worktree_branch_on_recreate` handling.

### 3. Write Characterization Tests

Before refactoring, add tests that capture current behavior, especially:
- Edge cases and failure paths
- Data that flows through failures
- Side effects that occur on different paths

## During Refactoring

### Preserve Return Type Semantics

```python
# WRONG - Loses failure context
def _try_reuse() -> Result | None:
    if failed:
        return None  # Lost: why did it fail?

# RIGHT - Preserves failure context
def _try_reuse() -> tuple[Result | None, str | None]:
    if failed:
        return (None, reason)  # Caller can still use the reason
```

### Use Structured Types Over Tuples

If a dataclass exists, use it. If returning multiple related values, create one.

```python
# Existing dataclass - USE IT
@dataclass
class ReuseResult:
    success: bool
    path: Path | None = None
    reason: str | None = None  # Failure reason preserved

# DON'T invent a different return pattern
def _helper() -> Path | None:  # Loses the reason field!
```

### Extract Pure Functions First

Order of extraction safety:
1. **Pure functions** (no side effects) - safest
2. **Functions with explicit outputs** (return values only) - safe
3. **Functions with side effects** (mutations, I/O) - careful
4. **Functions with implicit data flow** (failure reasons, context) - most dangerous

### Keep Related Logic Together

Don't split code that shares failure-path data:

```python
# WRONG - Split loses the connection
def _try_reuse():
    if failed:
        return None  # Reason lost here

def create():
    if _try_reuse() is None:
        # Can't access the reason anymore!
        handle_recreate(???)

# RIGHT - Keep failure context connected
def _try_reuse() -> tuple[Result | None, str | None]:
    if failed:
        return (None, reason)

def create():
    result, reason = _try_reuse()
    if result is None and reason:
        handle_recreate(reason)  # Context preserved
```

## After Refactoring

### Verify Behavioral Equivalence

1. Run existing tests (necessary but not sufficient)
2. Manually verify failure paths still produce same outcomes
3. Check that data used downstream is still available
4. Grep for variables that were in scope before extraction

### Check for Lost Data

After extracting `_helper()` from `main()`, verify:
- Every variable `main()` used after the extracted block is still accessible
- Return values from the extracted code include all needed data
- Failure paths in `_helper()` provide same context as before

## Common Pitfalls

| Pitfall | Example | Prevention |
|---------|---------|------------|
| Dropping failure context | `return None` vs `return (None, reason)` | Map data flow before extracting |
| Simplifying return types | 7-tuple to simple value | Keep full return structure |
| Losing scope variables | Variable used after extraction point | Check all downstream usages |
| Splitting related failure handling | Cleanup separated from reason | Keep failure paths atomic |

## Checklist

Before submitting refactored code:

**Meaningful extraction:**
- [ ] Each helper represents a phase I'd describe to a colleague
- [ ] Helper names describe *purpose*, not mechanics ("validate_preconditions" not "check_part_1")
- [ ] Coordinator reads like a table of contents
- [ ] Considered `noqa` for inherently complex coordinators

**Data flow preservation:**
- [ ] Mapped all data flows including failure paths
- [ ] Return types preserve all original semantics
- [ ] Failure reasons/context still available to callers
- [ ] No variables lost that were used downstream

**Verification:**
- [ ] Characterization tests added for edge cases
- [ ] Existing tests still pass
- [ ] Manually verified failure path behavior unchanged
