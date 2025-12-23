# AI Next Steps

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

**Status**: `IssueKey` and `GitHubIssueResolver` were created but not fully wired.

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

**Status**: `IssueKey` protocol created, `GitHubIssueKey` and `FakeIssueKey` implementations exist. `PendingRework` now uses `IssueKey`. Not fully propagated.

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
| `dependency_evaluator.py` | Check issue dependencies | YES - partially |
| `completion_processor.py` | Handle agent-done | YES |
| `reconciliation.py` | State reconciliation | Unclear |
| `session_controller.py` | Session state machine | Unclear |
| `transition_guard.py` | State transition validation | YES |
| `validation.py` | Publish/agent gates | YES |

### domain/ modules:

| Module | Purpose | Status |
|--------|---------|--------|
| `issue_key.py` | IssueKey protocol + implementations | DONE |
| `dependencies.py` | Parse dependency refs | Partial - drops M1-011 refs |
| `state_machines/` | FSM definitions | Exist |

### execution/ adapters:

| Module | Purpose | Status |
|--------|---------|--------|
| `github_adapter.py` | GitHub via gh CLI | Used |
| `github_issue_resolver.py` | IssueKey resolution | Created, not wired into deps |
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

### NOT Done (Critical):
- [ ] Wire Planner into `run_loop()`
- [ ] Wire SessionManager (replace `_create_session`, `_session_exists`, `_kill_session`)
- [ ] Wire LabelSync (replace `_sync_label_*` methods)
- [ ] Move GitHub-specific code from orchestrator to adapter
- [ ] Wire `IssueResolver` into dependency evaluation
- [ ] Remove dead code (~400-500 lines expected reduction)

---

## Commits Not Pushed

4 local commits ahead of origin:
1. `refactor: Reduce GitHub API polling, add refresh command, rename to repository_host`
2. `refactor: Make PendingRework store-agnostic with IssueKey`
3. `refactor: Add create_issue_key to RepositoryHost protocol`
4. `docs: Add AI-NEXT.md with pending architectural work`

Pre-push hook requires typecheck. Missing optional dependencies cause false positives.

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
