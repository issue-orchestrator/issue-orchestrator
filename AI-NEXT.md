# AI Next Steps

## Session 2025-12-24: Agent-Specific Completion Files (FIXED)

### Root Cause (Fixed)
Race condition: Review session and issue session shared the same `completion.json` file.

### Fix Implemented (COMPLETE)
Each agent writes to its own completion file based on agent name:
- Issue agent: `.issue-orchestrator/completion-agent_e2e-test.json`
- Review agent: `.issue-orchestrator/completion-agent_e2e-test-approves.json`
- Second review (after rework): `.issue-orchestrator/completion-agent_e2e-test-approves-2.json`

**Files changed:**
1. `models.py` - Added `get_completion_path(agent_name)` function
2. `agent_done.py` - Reads `ORCHESTRATOR_COMPLETION_PATH` env var, adds numeric suffix if file exists
3. `orchestrator.py` - Sets `ORCHESTRATOR_COMPLETION_PATH` env var and `Session.completion_path` field
4. `Session` dataclass - Added `completion_path` field
5. `session_controller.py` - Passes `completion_path` to completion_processor
6. `completion_processor.py` - Accepts optional `completion_path` parameter
7. **`observation/observer.py`** - Fixed to use `session.completion_path` instead of hardcoded path (2025-12-24)

### ~~CURRENT BUG: Session completion not detected~~ FIXED

**Root cause:** `observer.py` line 136 was hardcoded to look for `.issue-orchestrator/completion.json` instead of using `session.completion_path`.

**Fix:** Changed to `session.worktree_path / session.completion_path`

### Remaining Work
- [ ] **DRY: Create shared Python utility for issue creation in tests** - currently duplicated across test files
- [ ] Reduce test timeouts from 30min to 10min

### BUG: Orchestrator loop showing impossibly high iteration counts

Logs show 14M+ iterations:
```
[LOOP] Iteration 14902829 - active=0, pending_reviews=0, paused=False
```

**Mystery:** There IS a hardcoded 10-second sleep at `orchestrator.py:1645`:
```python
await asyncio.sleep(10)
```

At 10 seconds/iteration, 14M iterations would take ~4.5 years. A 4-minute test should have ~27 iterations max.

**Possible causes:**
- Loop counter persisting across runs (but it's initialized to 0 in `run_loop()`)
- Sleep not actually being awaited
- Multiple loop instances running concurrently
- Log from a different/old orchestrator process

**Needs investigation** - not a sleep-missing bug, something else is wrong.

---

## Archived: Session 2025-12-23 Evening: Event Emission & Validation Cache

### Completed Work

1. **Subprocess Event Emission** (`emit.py` - NEW)
   - Fire-and-forget event emission via IPC socket
   - Subprocesses (validation hooks) can emit events to orchestrator
   - Uses `ORCHESTRATOR_IPC_SOCKET` env var

2. **IPC Server Enhanced** (`ipc/server.py`)
   - Added `on_event` callback to receive events from clients
   - Added `set_event_handler()` for deferred wiring
   - Broadcasts received events to all connected clients

3. **Event Wiring** (`bootstrap.py`)
   - Subprocess events flow through PluggyEventSink
   - Events visible to SSE clients and other listeners

4. **Validation Events** (`control/validation.py`)
   - `validation.started` - when validation begins
   - `validation.completed` - when validation ends (with pass/fail/duration)
   - `validation.cache_hit` / `validation.cache_miss` - cache lookup results

5. **Simplified Validation Cache** (`control/validation.py`)
   - **BREAKING**: Changed from per-suite paths to single path per SHA
   - Old: `.issue-orchestrator/validation/{suite}/{sha}.json`
   - New: `.issue-orchestrator/validation/{sha}.json`
   - Cache now checks command match - agent_gate and publish_gate share cache if same command

6. **Agent IPC Socket** (`control/isolation.py`)
   - `ORCHESTRATOR_IPC_SOCKET` env var set for all agent sessions
   - Agents can emit events back to orchestrator

7. **DRY Config Lookup** (`config.py`)
   - New `find_config_file()` - single source of truth for config lookup
   - New `load_validation_config()` - lightweight loader for validation hooks
   - Updated `agent_done.py` and `prepush_check.py` to use shared lookup

8. **E2E Cleanup Fix** (`tests/e2e/conftest.py`)
   - Added `_cleanup_tmux_sessions()` to kill zombie e2e test windows
   - Prevents accumulation of orphan tmux windows

### Created Issues

- **#569 [M3-003]** - Add cleanup adapter for test session cleanup (tmux/iTerm2)

### Status

- `make typecheck` passes
- Unit tests need verification (were interrupted)
- E2E tests need verification

### Key Design Decision: Unified Validation Cache

The validation cache is now **command-aware**:
- One file per SHA: `{worktree}/.issue-orchestrator/validation/{sha}.json`
- Record includes the `command` field
- Cache hit only if SHA matches AND command matches
- This means: if agent_gate and publish_gate use the same command, validation runs ONCE

---

## Session 2025-12-23 Findings (FIXED)

### ~~IMMEDIATE FIX REQUIRED: GitHubAdapter.create_issue_key() is BROKEN~~

**FIXED** in commit `223facf` (2025-12-23). The method now correctly:
1. Fetches the issue via `get_issue(issue_number)`
2. Parses external_id from title using `parse_external_id()`
3. Falls back to issue number only if no external_id found

---

### What's Actually Working (verified by code review)

1. **IssueKey protocol** (`domain/issue_key.py`) - Clean, well-designed
2. **GitHubIssueResolver** (`execution/github_issue_resolver.py`) - Can cache M1-011 → issue_number
3. **Dependency parsing** (`domain/dependencies.py`) - `parse_dependency_refs()` understands M1-011 syntax
4. **DependencyEvaluator** (`control/dependency_evaluator.py`) - Full resolution pipeline using IssueResolver
5. **Bootstrap wiring** - GitHubIssueResolver is created and passed to DependencyEvaluator
6. **PendingRework** - Uses IssueKey (but gets broken keys due to bug above)

### What's NOT Working

1. **GitHubAdapter.create_issue_key()** - BROKEN (see above)
2. **Orchestrator de-godding** - Previous session claimed to do this but didn't actually wire anything
3. **control/planner.py** - EXISTS but run_loop() doesn't use it
4. **control/session_manager.py** - EXISTS but orchestrator uses its own methods
5. **control/label_sync.py** - EXISTS but not used
6. **control/action_applier.py** - EXISTS but not called

---

### Execution Order for This Work

1. **Fix GitHubAdapter.create_issue_key()** - 5 min fix, unblocks everything
2. **Verify IssueResolver works end-to-end** - Write a test or manual check
3. **Wire Planner into run_loop()** - The big de-godding step
4. **Wire SessionManager** - Replace `_create_session`, `_session_exists`, `_kill_session`
5. **Wire LabelSync** - Replace `_sync_label_*` methods
6. **Remove dead code** - ~400-500 lines expected reduction
7. **Run tests** - `pytest tests/unit/ -v`

---

## Feedback 1: Top 3 Layer Disconnects to Fix

### 1. Orchestrator imports concrete adapters (violates own architecture claim)

**Problem**: `bootstrap.py` docstring claims "orchestrator core imports only Protocols (ports)" but `orchestrator.py` imports `GitHubAdapter` directly and can instantiate it internally.

**Fix**:
- Change orchestrator constructor to accept port types only
- Remove "instantiate adapter in orchestrator" fallback
- Do all wiring in `bootstrap.py` only

**Status**: Partially done - renamed to `_repository_host` but orchestrator still has GitHub-specific code in `scan_needs_rework_prs()`.

### 2. Dependency parsing supports external IDs but drops them

**Problem**: `domain/dependencies.py` regex matches `M1-011` style IDs but then explicitly skips them with "not yet supported" log.

**Fix**:
- Introduce `IssueResolver` interface at boundary
- `DependencyResolver.resolve_external_id("M1-011") -> issue_number | None`
- Implement using IssueTracker port

**Status**: Infrastructure exists (`IssueKey`, `GitHubIssueResolver`, `DependencyEvaluator`) and IS wired in bootstrap.py. BUT `GitHubAdapter.create_issue_key()` is broken - see "Start Here" section above.

### 3. Identity is implicit - model treats GH number as identity

**Problem**: Mental model implies `[M1-011]` is canonical identity, but `models.Issue` uses `number: int` as natural key everywhere.

**Fix**: Create explicit `IssueKey` type:
```python
@dataclass(frozen=True)
class IssueKey:
    external_id: str  # "M1-011"
    repo: str
    gh_number: int | None = None  # locator, not identity
```

**Status**: `IssueKey` protocol created and well-designed. `GitHubIssueKey` and `FakeIssueKey` implementations exist. `PendingRework` uses `IssueKey`. HOWEVER, keys are created with wrong data due to broken `create_issue_key()` - see "Start Here" section.

---

## Feedback 1: Additional Issues

### A. Orchestrator is a god-object (~2700 lines)

**Problem**: Mixes orchestration policy, session lifecycle, reconciliation, git subprocess behaviors, scheduling, observation, GitHub writes.

**Fix**: Split into named controllers:
- `control/planner.py` (choose work) - EXISTS but not wired
- `control/reconciler.py` (snapshot + compare-before-mutate)
- `control/publisher.py` (push/create PR/apply labels/comments)
- `control/session_lifecycle.py` (start/monitor/resume/stop) - EXISTS as `session_manager.py` but not wired
- `control/worktree_manager.py` (worktree CRUD + "existing work" detection)

**Status**: Modules created but NOT wired in. Orchestrator still does everything directly.

### B. Legacy modules + new adapters coexist confusingly

**Problem**: Both legacy adapters at package root (`github.py`, `tmux.py`) AND newer structured adapters under `execution/` that wrap the old stuff.

**Fix**:
- Make `execution/` the only "official" adapter surface
- Mark legacy modules as internal: rename to `_github_cli.py` etc or move under `execution/_impl/`
- Ensure `control/` and `orchestrator.py` never import legacy modules directly

### C. Two event systems compete

**Problem**: `domain/events.py` EventBus vs `ports/event_sink.py` TraceEvent vs pluggy lifecycle hooks.

**Fix**: Pick `EventSink` as canonical. Domain returns transition info (pure), control emits via EventSink.

---

## Feedback 2: IssueKey Design

### Key insight: Identity vs Locator

- **Identity** (what you promise the user): stable, semantic, portable → `M1-011`
- **Locator** (how you talk to GitHub): can change → issue number

### Recommended approach:

1. In domain/control logic: key by `external_id` (IssueKey)
2. In GitHub adapter layer: operate on `gh_number`
3. Maintain resolver: `external_id -> gh_number` (built at startup, updated incrementally)

### Minimal implementation:

1. `IssueKey(external_id, repo)` - DONE
2. `IssueResolver` service with `resolve(key) -> int` - DONE (`GitHubIssueResolver`)
3. Keep `models.Issue.number` for adapter calls - current state
4. Update dependencies to reference `external_id` and resolve at boundary - NOT DONE

---

## Existing Modules Inventory

### control/ modules that EXIST but are NOT fully wired:

| Module | Purpose | Wired? |
|--------|---------|--------|
| `planner.py` | Pure planning decisions, produces `Plan` | NO - run_loop doesn't use it |
| `action_applier.py` | Executes Plan actions | NO - exists but not called |
| `actions.py` | Action type definitions | Used by planner |
| `session_manager.py` | Session lifecycle | NO - orchestrator has own methods |
| `label_sync.py` | Label reconciliation | NO - orchestrator does directly |
| `label_projection.py` | Desired label state | NO - not used |
| `scheduler.py` | Issue prioritization | YES - used by planner |
| `dependency_evaluator.py` | Check issue dependencies | YES - fully wired, uses IssueResolver |
| `completion_processor.py` | Handle agent-done | YES |
| `reconciliation.py` | State reconciliation | Unclear |
| `session_controller.py` | Session state machine | Unclear |
| `transition_guard.py` | State transition validation | YES |
| `validation.py` | Publish/agent gates | YES |

### domain/ modules:

| Module | Purpose | Status |
|--------|---------|--------|
| `issue_key.py` | IssueKey protocol + implementations | DONE |
| `dependencies.py` | Parse dependency refs | DONE - `parse_dependency_refs()` supports M1-011 |
| `state_machines/` | FSM definitions | Exist |

### execution/ adapters:

| Module | Purpose | Status |
|--------|---------|--------|
| `github_adapter.py` | GitHub via gh CLI | Used |
| `github_issue_resolver.py` | IssueKey resolution | DONE - wired into DependencyEvaluator via bootstrap |
| `session_runner_adapter.py` | Terminal sessions | Used |
| `event_sink_adapter.py` | Event publishing | Used |

---

## Current State Summary

### Done:
- [x] `IssueKey` protocol in `domain/issue_key.py`
- [x] `GitHubIssueKey` and `FakeIssueKey` implementations
- [x] `GitHubIssueResolver` in `execution/`
- [x] `PendingRework` uses `IssueKey` instead of GitHub fields
- [x] `create_issue_key()` added to `RepositoryHost` protocol
- [x] Polling improvements (queue_refresh_seconds, refresh endpoint)
- [x] Planner module created with pure planning logic
- [x] `IssueResolver` wired into `DependencyEvaluator` via bootstrap.py
- [x] `parse_dependency_refs()` supports M1-011 syntax

### Recently Fixed:
- [x] **GitHubAdapter.create_issue_key()** - Fixed in commit 223facf (2025-12-23)
- [x] **Session completion not detected** - Observer was hardcoding path, now uses `session.completion_path` (2025-12-24)

### NOT Done (Critical - De-Godding):
- [x] Wire Planner into `run_loop()` - DONE, uses snapshot + plan + _apply_plan()
- [ ] Wire SessionManager (replace `_create_session`, `_session_exists`, `_kill_session` in orchestrator.py lines 782-806)
- [ ] Wire LabelSync (replace `_sync_label_*` methods in orchestrator.py lines 613-672)
- [x] Wire ActionApplier - DONE as `_apply_plan()` + `_execute_launch_action()` etc
- [ ] Move GitHub-specific code from orchestrator to adapter (e.g., `scan_needs_rework_prs()`)
- [ ] Remove dead code - orchestrator still ~2900 lines
- [ ] Consolidate event systems (EventSink as canonical)

---

## Other Items

### E2E Test Cleanup
- Old e2e-test issues accumulating in GitHub
- Need cleanup mechanism

### Rate Limit Detection
- GitHub returns rate limit in response headers
- Could add awareness without extra API calls

---

## Architecture Rule

```
Planner (pure)           Orchestrator (impure)        Adapters (I/O)
"Should we?"             "Can we? When?"              "How?"
```

**Rule of thumb:**
- "Should we?" → Planner
- "Can we?" → Orchestrator
- "How?" → Adapters

---

## Specific Wiring Instructions (from plan file)

### Wire Planner into run_loop:

```python
# BEFORE (current - inline decision logic):
async def run_loop(self):
    while not self._shutdown:
        # 50+ lines of inline decision logic
        available = self.scheduler.get_available_issues(...)
        for issue in available[:slots]:
            self.launch_session(issue)
        self.process_pending_reviews()
        ...

# AFTER (clean separation):
async def run_loop(self):
    while not self._shutdown:
        snapshot = self._create_snapshot()
        plan = self.planner.plan(snapshot)
        self._apply_plan(plan)  # Use action_applier.py
        await asyncio.sleep(self.config.poll_interval)
```

### Wire SessionManager:

Replace in orchestrator:
- `_create_session()` → `session_manager.start(ctx)`
- `_session_exists()` → `session_manager.exists(ref)`
- `_kill_session()` → `session_manager.stop(ref)`

### Wire LabelSync:

Replace `_sync_label_*` methods with:
```python
def _handle_state_change(self, event) -> None:
    desired = self.label_projection.for_issue_state(event.new_state)
    current = self.repository_host.get_issue_labels(event.issue_number)
    self.label_sync.sync(event.issue_number, current, desired)
```

---

## Reference: Plan File Location

Full plan at: `~/.claude/plans/ancient-drifting-neumann.md`
