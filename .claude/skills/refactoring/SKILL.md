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

## The Golden Rule

**Refactoring changes structure, not behavior.** If tests pass before and after but behavior changed, your tests have gaps.

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

- [ ] Mapped all data flows including failure paths
- [ ] Return types preserve all original semantics
- [ ] Failure reasons/context still available to callers
- [ ] No variables lost that were used downstream
- [ ] Characterization tests added for edge cases
- [ ] Existing tests still pass
- [ ] Manually verified failure path behavior unchanged
