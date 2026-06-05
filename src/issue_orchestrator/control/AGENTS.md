# Control

**Purpose**: Orchestration logic - the "brain" that coordinates system behavior.

**Boundaries**:
- Uses domain types and port interfaces only
- Never imports from `adapters/` or `execution/` directly
- Follows Observer → Planner → ActionApplier pattern (see ADR-0014)
- Planner generates actions, ActionApplier executes them - never bypass this

## Strongly Typed Session Run Ownership

Active session control paths must preserve a typed, explicit data flow for run
assets. The owner that creates a session run owns filesystem discovery and
allocation; every lower-level collaborator receives the resulting typed values
through constructors or method arguments.

Required invariants:
- Leaf functions declare the narrow typed artifact contract they need. If a
  function needs only validation stdout/stderr/record paths, pass that typed
  leaf contract directly rather than a broad run object.
- Group leaf contracts into strongly typed aggregates only where the grouping
  proves a real invariant, such as all paths belonging to the same owned
  session run.
- Active `Session` creation requires a frozen typed run object such as
  `SessionRunAssets`; do not model ownership as a naked `Path`, `Path | None`,
  loose string, default value, or rediscoverable hint.
- The run owner allocates `run_dir` and any exchange assets, records them, and
  injects the typed run object into lower-level collaborators. Lower-level
  objects may unpack paths only at filesystem I/O edges; they must not rummage
  through the worktree to rediscover them.
- Review exchange code follows the same rule: construct a typed
  `ReviewExchangeRun` / `ReviewExchangeRunAssets` at the owning boundary, then
  pass that object through the exchange runner, completion processor, cache, and
  artifact writers.
- If the typed contract cannot be satisfied, fail fast or skip restoration of
  that incomplete historical record. Do not silently manufacture a replacement
  run directory.
- No active control path may use "latest run", completion-path inference,
  alternate session names, session-name search, or worktree scans as fallback
  recovery for a missing `run_dir`.
- Filesystem search helpers such as `find_run_dir(...)` are not part of active
  session ownership. If they remain for explicit UI/debug/historical inspection,
  keep them outside active control flow and name the behavior as best-effort
  inspection.
- Avoid weak metadata maps for owned contracts. Prefer frozen dataclasses,
  enums, value objects, and required constructor arguments over loose
  `dict[str, str]`, optional fields, or sentinel values.
- Tests for active session and review exchange flows should inject the typed run
  assets directly. Fakes should fail if active paths attempt fallback discovery,
  so ownership regressions are caught immediately.

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
