# Plan: Abstraction Improvements for Maintainability

*Created: 2026-01-10*
*Status: Proposed*

## Overview

Three targeted improvements to reduce code duplication and improve maintainability:

1. **Error handling helper** for action_applier.py (~100 lines saved)
2. **Session naming consolidation** (use existing `parse_terminal_id` everywhere)
3. **Generic WorkflowDecision\<T\>** (unify 3 nearly-identical classes)

---

## 1. Error Handling Helper

### Problem
14+ try/except blocks in `action_applier.py` follow identical patterns:
```python
try:
    self.labels.add_label(action.issue_number, action.label)
    logger.info(issue_log(action.issue_number, "Label added: %s"), action.label)
    return ActionResult.ok(action, issue_number=action.issue_number, label=action.label)
except Exception as e:
    logger.error(issue_log(action.issue_number, "Failed to add label %s: %s"), action.label, e)
    return ActionResult.fail(action, str(e))
```

### Solution
Create a helper function (not decorator - simpler for this case):

**File:** `src/issue_orchestrator/control/action_applier.py`

```python
def _safe_execute(
    self,
    action: Action,
    operation: Callable[[], None],
    success_log: str,
    error_log: str,
    issue_number: int,
    **result_kwargs,
) -> ActionResult:
    """Execute operation with standard error handling."""
    try:
        operation()
        logger.info(issue_log(issue_number, success_log))
        return ActionResult.ok(action, issue_number=issue_number, **result_kwargs)
    except Exception as e:
        logger.error(issue_log(issue_number, error_log), e)
        return ActionResult.fail(action, str(e))
```

**Usage:**
```python
def _apply_add_label(self, action: AddLabelAction) -> ActionResult:
    # ... reconciliation check ...
    return self._safe_execute(
        action=action,
        operation=lambda: self.labels.add_label(action.issue_number, action.label),
        success_log=f"Label added: {action.label}",
        error_log=f"Failed to add label {action.label}: %s",
        issue_number=action.issue_number,
        label=action.label,
    )
```

### Files to Modify
- `src/issue_orchestrator/control/action_applier.py`
  - Add `_safe_execute` helper method
  - Refactor `_apply_add_label`, `_apply_remove_label`, `_apply_remove_worktree`

### Scope
Start with simple cases (add/remove label, remove worktree). More complex handlers with error accumulation patterns stay as-is for now.

---

## 2. Session Naming Consolidation

### Problem
Centralized parsing exists in `adapters/terminal/naming.py`:
- `parse_terminal_id(tid)` → `ParsedSessionName`
- `number_from_terminal_id(tid)` → `int | None`

But 6+ locations use ad-hoc parsing instead:
- `observer.py:76-87` - uses `.startswith()` + `.replace()`
- `completion_handler.py:399` - raw regex
- `completion_handler.py:585` - `.split("-", 1)[0]`
- `session_controller.py:224` - `.replace("review-", "")`
- `cli.py:597` - `.replace("issue-", "")`

### Solution
Replace ad-hoc parsing with centralized functions. No new abstraction needed.

### Files to Modify
| File | Line | Change |
|------|------|--------|
| `observation/observer.py` | 76-87 | Use `number_from_terminal_id()` |
| `control/completion_handler.py` | 399 | Use `parse_terminal_id()` |
| `control/completion_handler.py` | 585 | Use `parse_terminal_id().session_type` |
| `control/session_controller.py` | 224 | Use `number_from_terminal_id()` |
| `entrypoints/cli.py` | 597 | Use `number_from_terminal_id()` |

### Import to Add
```python
from issue_orchestrator.adapters.terminal.naming import (
    parse_terminal_id,
    number_from_terminal_id,
)
```

---

## 3. Generic WorkflowDecision\<T\>

### Problem
Three nearly-identical decision classes:

```python
# review_workflow.py
@dataclass(frozen=True)
class ReviewDecision:
    should_launch: bool = False
    reviews_to_launch: tuple[PendingReview, ...] = field(default_factory=tuple)
    skip_reason: Optional[str] = None
    available_capacity: int = 0

# rework_workflow.py
@dataclass(frozen=True)
class ReworkDecision:
    should_launch: bool = False
    reworks_to_launch: tuple[PendingRework, ...] = field(default_factory=tuple)
    skip_reason: Optional[str] = None
    available_capacity: int = 0

# triage_workflow.py
@dataclass(frozen=True)
class TriageDecision:
    should_launch: bool = False
    triage_to_launch: tuple[PendingTriageReview, ...] = field(default_factory=tuple)
    skip_reason: Optional[str] = None
    available_capacity: int = 0
```

### Solution
Create generic base class:

**File:** `src/issue_orchestrator/control/workflows/workflow_decision.py` (NEW)

```python
from dataclasses import dataclass, field
from typing import Generic, TypeVar, Optional, Sequence

T = TypeVar("T")

@dataclass(frozen=True)
class WorkflowDecision(Generic[T]):
    """Generic decision for queue-based workflow launches."""

    should_launch: bool = False
    items_to_launch: tuple[T, ...] = field(default_factory=tuple)
    skip_reason: Optional[str] = None
    available_capacity: int = 0

    @classmethod
    def skip(cls, reason: str) -> "WorkflowDecision[T]":
        return cls(should_launch=False, skip_reason=reason)

    @classmethod
    def launch(cls, items: Sequence[T], capacity: int) -> "WorkflowDecision[T]":
        return cls(
            should_launch=True,
            items_to_launch=tuple(items),
            available_capacity=capacity,
        )
```

**Type Aliases:**
```python
# In each workflow file
ReviewDecision = WorkflowDecision[PendingReview]
ReworkDecision = WorkflowDecision[PendingRework]
TriageDecision = WorkflowDecision[PendingTriageReview]
```

### Files to Modify
| File | Changes |
|------|---------|
| `control/workflows/workflow_decision.py` | NEW - Generic class |
| `control/workflows/review_workflow.py` | Replace class with type alias, update field access |
| `control/workflows/rework_workflow.py` | Replace class with type alias, update field access |
| `control/workflows/triage_workflow.py` | Replace class with type alias, update field access |
| `control/planner.py` | Update field access (`reviews_to_launch` → `items_to_launch`) |

### Breaking Change
Field names change: `reviews_to_launch` → `items_to_launch`. All usages need updating.

### Keep Separate
- `EscalationDecision` - different pattern (escalate vs launch)
- `BatchTriageDecision` - different pattern (trigger vs launch, has cooldown)

---

## Implementation Order

1. **Error handling helper** (lowest risk, immediate benefit)
2. **Session naming consolidation** (no new code, just use existing)
3. **WorkflowDecision\<T\>** (higher impact, more files to change)

---

## Verification

### Error Handling
```bash
pytest tests/unit/test_action_applier.py -v
```

### Session Naming
```bash
pytest tests/unit/test_observer.py tests/unit/test_completion_handler.py tests/unit/test_session_controller.py -v
```

### WorkflowDecision
```bash
pytest tests/unit/test_review_workflow.py tests/unit/test_rework_workflow.py tests/unit/test_triage_workflow.py tests/unit/test_planner.py -v
```

### Full validation
```bash
make validate
```

---

## Estimated Impact

| Change | Lines Saved | Files Changed | Risk |
|--------|-------------|---------------|------|
| Error handling helper | ~60 | 1 | Low |
| Session naming consolidation | ~30 | 5 | Low |
| WorkflowDecision\<T\> | ~80 | 5 | Medium |
| **Total** | **~170** | **11** | |
