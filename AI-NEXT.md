# AI Next Steps

## Current Work: SessionKey Implementation (2025-12-28)

## Let's Try Everything (Prioritized)
1. **New worktree per session** (no reuse): issue/review/rework each get a fresh worktree (branch is source of truth).
2. **Unified e2e config**: single orchestrator config covers review + rework + triage flows.
3. **Claude `-p` mode**: run non-interactive for deterministic output and improved logging.

### Problem
E2E test bug "Can't trigger event approve from state approved!" - state machine error caused by session identity being conflated with terminal naming and GitHub issue numbers.

### Solution: SessionKey Abstraction
Proper domain identity for sessions: `SessionKey = (IssueKey, TaskKind)`

**Plan file:** `~/.claude/plans/curious-floating-hoare.md`

### Status

**Phase 1: Domain Layer** ✅ COMPLETE
- Created `src/issue_orchestrator/domain/session_key.py` with TaskKind enum and SessionKey dataclass
- Created `tests/unit/test_session_key.py` with 20 tests

**Phase 2: Session Model Update** ✅ COMPLETE
- Renamed `tmux_session_name` → `terminal_id` across all files
- Added `key: SessionKey` field to Session model
- Updated all Session creation sites:
  - `session_launcher.py`: CODE, REVIEW, REWORK task types
  - `session_restorer.py`: CODE and REVIEW based on is_review flag
  - `cli.py`: Adopted sessions as CODE
  - All test files use `FakeIssueKey` for test isolation

**Phase 3: Ports/Adapters** ✅ COMPLETE
- Added `session_exists_by_name()` and `send_to_session_by_name()` to SessionRunner port
- Updated hookspec, adapters, and tmux plugin

**Phase 4: Control Layer Bug Fixes** ✅ COMPLETE
- Fixed observer.py to use `terminal_id` instead of `issue.number` for lookups
- Fixed orchestrator.py session removal to use `terminal_id`

**Phase 5: Reconciliation Logic** ⏳ NOT STARTED
- Update `control/session_restorer.py` to use SessionKey for matching orphans
- Parse terminal_id → extract SessionKey in adapter layer
- Implement mismatch policy: work persisted → safe to kill; not persisted → quarantine

**Phase 6: Draft PR Policy** ⏳ NOT STARTED
- Create PRs with `--draft` flag
- Orchestrator promotes to ready after review passes
- Handle reconciliation for reviewed issues with draft PRs still open

### Test Status
- Unit tests: 1591 pass ✅
- Integration tests: 60 pass ✅
- E2E tests: **FAILING** - agent sessions dying quickly (see below)

### E2E Test Infrastructure Improvements (2025-12-28)
Added these improvements to `tests/e2e/conftest.py`:

1. **Persistent Log File** - Survives Ctrl+C
   - Logs to `/tmp/e2e-orchestrator-logs/e2e-{timestamp}.log`
   - All orchestrator output captured (not just filtered keywords)
   - Keeps last 10 log files, auto-cleans older ones

2. **Debug Paths Printed at Startup**
   ```
   [E2E DEBUG PATHS]
     Orchestrator log: /tmp/e2e-orchestrator-logs/e2e-20251228-021610.log
     Worktrees:        /tmp/e2e-worktrees
     Claude logs:      ~/.claude/logs
   ```

3. **Early Failure Detection**
   - Added `FATAL_ERROR_PATTERNS` - stops polling immediately on:
     - `"Traceback (most recent call last):"`
     - `"Can't trigger event"` (state machine errors)
     - `"FATAL:"`, `"panic:"`, `"RecursionError:"`
   - Raises `RuntimeError` with log file path

4. **Progress Indicators**
   - Every 10 seconds during waits:
     ```
     ... waiting for label 'completed' on issue #123 (30s elapsed, 90s remaining)
     ```

5. **Faster Polling**
   - Reduced default interval from 5s to 2s

6. **Fixed permission_mode**
   - Added `permission_mode="bypassPermissions"` to e2e agent config
   - Required for non-interactive Claude Code execution

### E2E Test Current Issue
Agent sessions are failing after ~14 seconds without completing:
- Sessions show "bypass permissions on" (correct - permissions bypassing works)
- Claude appears to run but doesn't call `agent-done`
- Some sessions timeout after 15 min (agent runs but doesn't complete)
- Other sessions fail quickly (~14 sec)

**Possible causes to investigate:**
1. Agent prompt timing - Claude might need more time to process
2. Hook blocking - PreToolUse hooks might be interfering
3. Worktree setup - something in the worktree might cause issues

### Next Step
Debug why agent sessions fail quickly:
1. Check Claude Code logs: `~/.claude/logs/`
2. Check worktree session logs: `/tmp/e2e-worktrees/issue-orchestrator-{N}/.issue-orchestrator/session.log`
3. Try running agent manually in a worktree to see what happens

---

## Previous Work: E2E Test Reliability (2025-12-27)

### Problem
E2E tests failing because issues created mid-test weren't picked up by orchestrator. Root cause: `queue_refresh_seconds` defaults to 600s (10 min).

### Solution: Control API (Orthogonal to UI Mode)
Created `control_api.py` - a lightweight HTTP API that runs in ALL modes (tmux, iterm2, web), not just web mode.

**Files:**
- `src/issue_orchestrator/control_api.py` (NEW) - FastAPI app on port 19080
- `src/issue_orchestrator/config.py` - Added `control_api_port: int = 19080`
- `src/issue_orchestrator/cli.py` - Added `--api-port` flag, starts API in all modes

**Endpoints:** `/api/refresh`, `/api/pause`, `/api/resume`, `/api/status`, `/api/shutdown`, `/api/health`

**E2E test updates:**
- `tests/e2e/conftest.py` - `trigger_refresh(port=19080)` uses control API
- `tests/e2e/test_full_pipeline.py` - Calls `trigger_refresh()` after creating issues

### Status
- [x] Control API implementation complete
- [x] E2E tests updated to use control API
- [ ] Run full e2e test to completion (last run showed all 3 issues processed, interrupted before assertions)

---

## Priority: Architectural Improvements (from feedback1.rtf)

These are the "calling card" level improvements that turn layer disconnects into deliberate design.

### A) Orchestrator imports concrete adapters - FIXED
- [x] Orchestrator now accepts port types, not GitHubAdapter directly
- [x] Fallback instantiation removed; all adapters come from bootstrap.py
- [x] `_repository_host` injected via build_orchestrator()

### B) Dependency parsing supports external IDs - FIXED
- [x] `DependencyEvaluator` uses `parse_dependency_refs()` which captures M1-011 refs
- [x] `GitHubIssueResolver` resolves external_id → issue_number
- [x] Wired into bootstrap.py: `issue_resolver` passed to `DependencyEvaluator`
- Note: legacy `parse_dependencies()` only used for display formatting in CLI

### C) Identity model (IssueKey) - IMPLEMENTED
- [x] `domain/issue_key.py`: IssueKey protocol + GitHubIssueKey implementation
- [x] `ports/issue_resolver.py`: IssueResolver protocol
- [x] `execution/github_issue_resolver.py`: Cached resolver with external_id → issue_number
- [x] `domain/issue_key.py:parse_external_id()`: Title parsing for [M1-011] prefixes

### D) Legacy modules coexist with new adapters - DONE
- [x] **Make execution/ the only "official" adapter surface**
- [x] Renamed legacy modules to internal helpers:
  - `github.py` → `_github_impl.py`
  - `tmux.py` → `_tmux_impl.py`
  - `iterm2.py` → `_iterm2_impl.py`
  - `worktree.py` → `_worktree_impl.py`
- [x] Updated all imports to use underscore-prefixed names

### E) Orchestrator.py is still a god-object (~2725 lines)

**Already exists in `control/`:**
- `control/planner.py` - choosing work (18KB)
- `control/reconciliation.py` - reconciliation logic (7KB)
- `control/session_manager.py` - session state management (10KB)
- `control/session_controller.py` - session control decisions (9KB)
- `control/completion_processor.py` - processing completions (18KB)
- `control/action_applier.py` - applying actions from planner (13KB)
- `control/scheduler.py` - scheduling logic (12KB)
- `control/label_sync.py` - label synchronization (5KB)
- `control/dependency_evaluator.py` - dependency checking (8KB)
- `control/workflows/` - review, rework, triage workflows

**Still embedded in orchestrator.py that should be extracted:**
- [ ] Session launching logic (~200 lines) → delegate more to `session_controller.py`
- [ ] Review/rework/triage session launching → delegate to workflows
- [x] PR scanning (`scan_needs_code_review_prs`, `scan_needs_rework_prs`) → now stores facts for Planner
- [x] Cleanup logic (`process_deferred_cleanups`) → now handled by Planner via CleanupFacts
- [ ] Orphan cleanup logic (`_recover_orphaned_cleanups`) → new `cleanup_controller.py`
- [ ] Run loop itself should be thinner - just: plan → apply → observe → repeat

**Key remaining extractions:**
- [ ] `launch_session` + related helpers → `session_controller.py`
- [ ] `launch_review_session`, `launch_rework_session` → workflow controllers
- [x] `reconcile_orphaned_pr_labels` → `LabelSync` (done)
- [ ] Main loop (`run_loop`) → should just call controllers, not contain logic

**Progress:**
- [x] Dead code removal: 2924 → 2725 lines (-199)
- [x] PRScanner extraction: 2725 → 2665 lines (-60)
- [x] HookVerifier extraction + stale comments: 2665 → 2033 lines (-632)
- [x] EventBus removal + triage-to-planner: 2033 → 1945 lines (-88)
- [x] Orchestrator-mediator refactor session (2025-12-25):
  - Extracted tick() pattern from run_loop
  - Added StateMachineManager for centralized state machine access
  - Simplified _apply_plan using dispatch table pattern
  - Made PRScanner and SessionRestorer injectable via bootstrap
  - Fixed observer wiring tests to use mock session_runner
  - Moved `reconcile_orphaned_pr_labels` → `LabelSync` (1593 → 1526)
  - Moved `_escalate_to_needs_human` → `ActionApplier` (full escalation flow)
  - Made StateMachineManager, CompletionProcessor, SessionController injectable via bootstrap
  - State machine dicts now delegate to StateMachineManager (single source of truth)
- [x] Orchestrator-mediator refactor continuation (2025-12-26):
  - Removed `queue_code_review` (legacy, replaced by discovered_reviews pattern)
  - Removed dead label sync helpers (`_sync_label`, `_remove_blocked_labels`)
  - Simplified `_session_launcher_callback` using dispatch pattern (85→45 lines)
  - Consolidated small state update handlers into `_update_state_after_action`
  - Simplified `_handle_queue_*_state_update` handlers (~90→30 lines)
  - Moved label adding to ActionApplier (added `code_review_label` to QueueReviewAction)
  - Updated test helpers to inject required components (session_controller, etc.)
  - Moved `_fetch_all_issues` logic → FactGatherer
  - Condensed property constructors (~60 → ~20 lines)
  - Condensed helper methods and removed verbose comments
  - Consolidated state update logic inline into `_update_state_after_action`
- **Current: 469 lines** (down from 2924, -2455 lines removed, -84%)

**Architecture status:**
- [x] 5 import-linter contracts enforced
- [x] ActionApplier is the only executor (via callbacks)
- [x] FactGatherer extracts fact gathering (including issue fetching)
- [x] Reconciliation in ActionApplier
- [x] PRScanner/SessionRestorer injectable via bootstrap
- [x] Orchestrator is now a pure-ish mediator (~469 lines)

**Goal ACHIEVED:** orchestrator.py is now ~469 lines (loop + wiring only)

### F) Two event systems compete - PARTIALLY COMPLETE
- [x] **Pick EventSink as canonical outward event stream**
- [x] Domain state machines return TransitionResult (pure) via `last_transition`
- [ ] Control emits events via EventSink after state machine transitions
- [x] Keep pluggy as an adapter/extension mechanism behind EventSink implementation
- Files modified:
  - `domain/state_machines/transition_result.py` - New TransitionResult dataclass
  - `domain/state_machines/session_machine.py` - Removed EventBus, uses TransitionResult
  - `domain/state_machines/issue_machine.py` - Removed EventBus, uses TransitionResult
  - `domain/state_machines/review_machine.py` - Removed EventBus, uses TransitionResult
  - `orchestrator.py` - Updated state machine creation (no EventBus param)
  - `tests/unit/test_state_machines.py` - Updated to test TransitionResult
- Remaining: Orchestrator should emit TraceEvents via EventSink after transitions

---

## Other Remaining Work

- [x] **Web-ui verified working** - all endpoints tested (2025-12-24)
- [ ] **DRY: Create shared `make_issue()` fixture in tests/conftest.py** - duplicated in 7 test files
- [ ] Consolidate event systems (EventSink as canonical) - see F above

### BUG: Orchestrator loop showing impossibly high iteration counts

Logs show 14M+ iterations:
```
[LOOP] Iteration 14902829 - active=0, pending_reviews=0, paused=False
```

At 10 seconds/iteration (hardcoded sleep at `orchestrator.py:1645`), 14M iterations = ~4.5 years.
A 4-minute test should have ~27 iterations max.

**Possible causes:**
- Loop counter persisting across runs (but initialized to 0 in `run_loop()`)
- Sleep not actually being awaited
- Multiple loop instances running concurrently
- Log from a different/old orchestrator process

---

## Recently Completed (2025-12-25)

### EventBus Removed - State Machines Pure - COMPLETE
- Created `TransitionResult` dataclass in `domain/state_machines/transition_result.py`
- Updated all state machines (Session, Issue, Review) to use `last_transition` instead of EventBus
- State machines are now pure - return `TransitionResult` instead of emitting events
- Removed EventBus import and creation from orchestrator
- Removed `_setup_event_handlers` and all `_on_*` handlers from orchestrator
- Callers should emit TraceEvents via EventSink after transitions
- 81 state machine tests, 119 orchestrator tests all pass

### Failure-to-Triage via Planner - COMPLETE
- Added `DiscoveredFailure` dataclass for session failures
- Planner now decides whether to queue triage review for failures
- Moved `_queue_triage_failure_review` logic from orchestrator to Planner
- New `_plan_discovered_failures` method produces `QueueTriageAction`
- New `_execute_queue_triage_action` method applies the action
- 5 new tests for failure-to-triage planning

### Startup Reconciliation Types - COMPLETE
- Added startup reconciliation types to `control/reconciliation.py`:
  - `StartupActionType` enum
  - `StartupAction`, `LocalSessionInfo`, `LocalState` dataclasses
  - `GitHubIssueInfo`, `GitHubPRInfo`, `GitHubState` dataclasses
  - `StartupFacts` for combined local + GitHub state
  - `StartupReconciler` class (follows Planner pattern)
- Ready for orchestrator startup method to use

### Legacy Module Renaming - COMPLETE
- Renamed legacy modules with underscore prefix to mark as internal:
  - `github.py` → `_github_impl.py`
  - `tmux.py` → `_tmux_impl.py`
  - `iterm2.py` → `_iterm2_impl.py`
  - `worktree.py` → `_worktree_impl.py`
- Updated all imports across src/ and tests/
- execution/ adapters are now the official interface

### Planner-Centric Decision Making - COMPLETE
- Converted `scan_needs_code_review_prs` to store `DiscoveredReview` facts for Planner
- Converted `scan_needs_rework_prs` to store `DiscoveredRework` and `DiscoveredEscalation` facts
- Converted `process_deferred_cleanups` to Planner via `CleanupFacts` → `CleanupSessionAction`
- Added `_gather_cleanup_facts()` and `_execute_cleanup_action()` to orchestrator
- Planner now handles: reviews, reworks, escalations, triage, and cleanups
- All decisions flow through Planner → Plan → apply pattern

### Orchestrator Size Reduction - ONGOING
- Extracted `_verify_hooks_on_startup` to `control/hook_verifier.py`
- Removed stale refactoring comments throughout codebase
- Removed EventBus and dead handlers
- Moved triage failure decision to Planner
- **orchestrator.py reduced from 2924 to 1945 lines (-979 lines, -33%)**

---

## Recently Completed (2025-12-24)

### Dead Code Removal
- Removed unused imports: `datetime`, `has_uncommitted_changes`, `ProcessingResult`, `SessionDecision`, `CompletionOutcome`, `SessionObservationResult`
- Removed dead methods: `_using_iterm2`, `_process_session_exit`, `_extract_session_number`, `process_pending_reviews`, `process_pending_triage_reviews`, `process_pending_reworks`
- Updated tests to remove references to deleted methods

### Agent-Specific Completion Files - FIXED
- Each agent writes to its own completion file based on agent name
- Observer now uses `session.completion_path` instead of hardcoded path

### SessionManager & LabelSync Wiring - COMPLETE
- Wired SessionManager into orchestrator (~15 call sites)
- Wired LabelSync into orchestrator (6 methods)

### GitHub-Specific Code Migration - COMPLETE
- Moved all `subprocess.run(["gh", ...])` calls from orchestrator to adapter
- Updated tests to use MockGitHubAdapter pattern

---

## Architecture Notes

### Layer Separation (Target State)
```
Domain (pure)            Control (decisions)      Adapters (I/O)
- State machines         - Planner               - GitHubAdapter
- IssueKey identity      - Reconciler            - TmuxAdapter
- Dependencies           - SessionLifecycle       - ITerm2Adapter
                         - Publisher              - WorktreeAdapter
```

### Composition
```
bootstrap.py → build_orchestrator() → wires all adapters and controllers
```

---

## Lower Priority Items

### PR #1026 Cleanup
- Should have labels: `blocked` or `failed-code-review`, `e2e-test`

### E2E Test Cleanup
- Old e2e-test issues accumulating in GitHub

### Rate Limit Detection
- GitHub returns rate limit in response headers
- Could add awareness without extra API calls

## Notes for Restart (e2e debugging)
- Root cause of rework e2e failures: worktree reuse disabled but existing worktree paths and branch locks caused git worktree add to fail. Fixes in `src/issue_orchestrator/_worktree_impl.py` now:
  - If `ORCHESTRATOR_DISABLE_WORKTREE_REUSE=1`, remove existing worktree path via `git worktree remove --force` (fallback to rmtree).
  - If branch already checked out elsewhere, detach that worktree (`git checkout --detach`) to free branch.
- Rework session PR detection bug: `launch_rework_session` used `get_prs_for_branch(f"{issue_number}-")` (too broad) → could pick wrong PR/branch. Fixed to `get_prs_for_issue(issue_number)`.
- Rework cycle labels were inconsistent: session launcher wrote `rework-N` but workflow/scanner expects `rework-cycle-N`. Fixed `_update_rework_cycle_label` to add/remove `rework-cycle-N`, and updated PR scanner to parse `rework-cycle-N`.
- E2E label setup: ensure `rework-cycle-1` and `rework-cycle-2` labels exist in `tests/e2e/conftest.py`.
- Added unit tests in `tests/unit/test_worktree.py` for new behavior: detaching branch + removing existing path when reuse disabled.
- Reran e2e rework test: `tests/e2e/test_real_scenarios.py::TestReworkCyclesAndEscalation::test_rework_cycles_lead_to_escalation` now PASSED (took ~12-22 min). Full e2e suite not rerun (command was rejected by user due to permissions).
- Pending: run full e2e suite under unlimited permissions.
