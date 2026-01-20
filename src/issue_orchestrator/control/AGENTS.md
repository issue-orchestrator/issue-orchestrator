# Control

**Purpose**: Orchestration logic - the "brain" that coordinates system behavior.

**Boundaries**:
- Uses domain types and port interfaces only
- Never imports from `adapters/` or `execution/` directly
- Follows Observer → Planner → ActionApplier pattern (see ADR-0014)
- Planner generates actions, ActionApplier executes them - never bypass this

## Writing Coordinator Functions

Control layer code often involves multi-step coordination (launching sessions, handling completions, etc.). Write these functions so they read like a **table of contents**:

```python
def launch_session(self, issue, active_sessions) -> LaunchResult:
    # Phase 1: Validate preconditions
    if result := self._check_preconditions(issue, active_sessions):
        return result

    # Phase 2: Acquire resources
    claim = self._acquire_claim(issue)
    if not claim.success:
        return claim.as_failure()

    # Phase 3: Prepare environment
    ctx = self._prepare_worktree(issue, claim)
    if ctx.error:
        self._release_claim(issue, claim)
        return LaunchResult(None, False, ctx.error)

    # Phase 4: Execute
    return self._start_terminal(issue, ctx, claim)
```

### When to Extract a Helper

Before extracting code into a helper, ask:

> "If I were explaining this function to a colleague, would I describe this as a distinct step?"

- **Yes** → Extract it with a name that describes the phase
- **No** → Keep it inline, possibly with a phase comment

### Keep Error Handling With Its Operation

Don't scatter error handling across helpers. If worktree creation can fail, handle that failure in the same place:

```python
# GOOD - Error handling visible with the operation
ctx = self._prepare_worktree(issue)
if ctx.error:
    self._cleanup(claim)
    return LaunchResult(None, False, f"Worktree failed: {ctx.error}")

# BAD - Error handling hidden in helper, caller doesn't see failure path
result = self._prepare_worktree_and_handle_errors(issue, claim)  # Magic!
```

### Complexity Metrics

If a coordinator function triggers C901/PLR0912 warnings after organizing into clear phases:

1. First, verify each phase is meaningful (not arbitrary splits)
2. If complexity is inherent to coordination, use `# noqa: C901, PLR0912` with a comment explaining why
3. Don't extract helpers just to satisfy metrics - clarity matters more
