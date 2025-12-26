# AI Next Steps

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
- **Current: 1526 lines** (down from 2924, -1398 lines removed, -48%)

**Remaining to extract (~1026 lines to reach 500 line goal):**
- `handle_session_completion` (95 lines) - mostly delegated to CompletionHandler
- `_session_launcher_callback` (91 lines) - session type dispatch
- `__post_init__` (90 lines) - could simplify with more injection
- `queue_code_review` (66 lines) - legacy, replaced by discovered_reviews pattern
- `_update_dependency_problems` (65 lines) - state update logic
- `scan_needs_rework_prs` (44 lines) - already delegates to PRScanner
- Various smaller methods (lazy properties could be made injectable)

**Architecture status:**
- [x] 5 import-linter contracts enforced
- [x] ActionApplier is the only executor (via callbacks)
- [x] FactGatherer extracts fact gathering
- [x] Reconciliation in ActionApplier
- [x] PRScanner/SessionRestorer injectable via bootstrap

**Goal:** orchestrator.py becomes ~400 lines (loop + wiring only)

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
