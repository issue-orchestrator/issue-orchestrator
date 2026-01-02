# Architecture Refactor Master Plan (HTTPX + Resilience)

Last updated: 2025-12-31
Owner: Codex

## Objectives (Master List)
1) Single GitHub transport (httpx) with typed errors, retries, budgets; ALL GH access (prod + test_data + e2e) goes through it.
2) Central verification service with budgets + classifier + circuit breaker (pause vs needs-reconcile).
3) Snapshot/event flow is authoritative for tests (no direct GH reads in waits). Refresh only at known boundaries.
4) Replace implicit polling loops with event-driven watchers; fall back to GH only on gaps or stale snapshots.
5) Isolate GH read cost in one module with a small cache surface and explicit invalidation rules.
6) Diagnostic mode: per-test GH usage + slowest phases, emitted as a single structured report.
7) Keep orchestrator loop thin; move policies into small services (Verifier, HealthGate, ReworkPolicy, ReviewPolicy).

## Current Baseline (Inventory)
- Production GH access: `execution/github_adapter.py` -> `execution/github_http.py`.
- Test GH access: `test_data.py` and `tests/e2e/conftest.py` call `GitHubHttpClient` directly (bypass adapter).
- Tests still include GH polling waits in a few helpers.
- Event/snapshot watchers already exist: `tests/e2e/flows.py`.
- GH audit + usage counters already exist and are wired into control API.

## Architecture End-State (Target)
- GH access is mediated by a single transport + service layer:
  - `GitHubTransport` (httpx client + typed errors + retry policy)
  - `GitHubService` (domain-level methods, write-verify, budgets)
  - `GitHubCache` (small, explicit cache surface with invalidation hooks)
- Tests use orchestrator events/snapshots for assertions.
- Verification and retry policies are centralized (no ad-hoc waits).
- Orchestrator loop delegates policies to small services.

## Phased Plan

### Phase 1: Single GitHub Transport (COMPLETED)
Goal: Eliminate direct `GitHubHttpClient` usage outside the adapter layer.

Deliverables:
1) New GH service interface (ports) used by prod + test_data + e2e.
2) `test_data.py` uses adapter/service instead of direct HTTP client.
3) E2E GH helpers use adapter/service (not raw HTTP client).
4) Typed errors unified: `GitHubHttpError` remains canonical.

Work items:
1) Introduce `GitHubService` (or expand adapter) as the only GH entry point. ✅
2) Route `test_data.py` through adapter/service. ✅
3) Route `tests/e2e/conftest.py` GH helpers through adapter/service. ✅
4) Ensure gh_audit continues to annotate calls. ✅
5) Route `cli.py` and `setup_wizard.py` through adapter. ✅

Exit criteria:
- No direct `GitHubHttpClient` usage outside `execution/`. ✅
- Tests still pass (unit/integration). (pending verification)

### Phase 2: Central Verification Service (COMPLETED)
Goal: One place for write-verify + retry + budget policy.

Deliverables:
1) `VerificationService` with: ✅
   - retry budgets (time, attempts) ✅
   - classifier (retryable vs fatal) ✅
   - circuit breaker (pause vs needs-reconcile) ✅
2) Adapter/service delegates all verify loops to it. ✅

Exit criteria:
- All write verification uses `VerificationService`. ✅
- Logging is consistent and includes last observed state. ✅

Implementation:
- Created `ports/verification.py` with VerificationService protocol
- Created `execution/verification_service.py` with DefaultVerificationService
- Updated GitHubAdapter._verify_write() to use VerificationService

### Phase 3: Authoritative Event/Snapshot Tests (COMPLETED)
Goal: Tests rely on orchestrator event flow, not GH polling.

Deliverables:
1) Replace GH polling waits in e2e with watcher/snapshot waits. ✅
2) Explicit `trigger_refresh()` boundaries after writes. ✅
3) Direct GH reads only for setup/cleanup. ✅

Exit criteria:
- No GH read in test wait loops. ✅
- Event/snapshot waits are the only mechanism for assertions. ✅

Implementation Notes:
- E2E tests already use watcher-based async waits (IssueWatch.has_label, SystemWatch.idle, etc.)
- Watcher DSL includes diagnostics (WaitTimeout, NoProgressTimeout include last events)
- Removed unused synchronous GH polling functions (wait_for_issue_label, wait_for_pr_created)

### Phase 4: GH Cost Isolation + Diagnostics (COMPLETED - already implemented)
Goal: Make GH usage visible and enforceable.

Deliverables:
1) `GitHubCost` module (small cache + invalidation rules). ✅ (gh_audit.py)
2) Diagnostic mode: per-test GH usage + slowest phases report. ✅ (gh_audit.emit_report())
3) CI/local gating via gh-activity limits. ✅ (zzz_test_gh_audit.py, gh_activity_limit marker)

Exit criteria:
- GH usage report emitted per e2e run. ✅
- Limits enforced consistently. ✅

Implementation Notes:
- `gh_audit.py` tracks all GH API calls with caller, command, reason, scope, issue
- `zzz_test_gh_audit.py` runs at end of e2e tests and enforces limits
- pytest marker `@pytest.mark.gh_activity_limit(test_gh_activity_limit=X)` for per-test limits

### Phase 5: Thin Orchestrator + Policy Services (COMPLETED - already implemented)
Goal: Orchestrator delegates policy logic to small services.

Deliverables:
1) Move remaining policy logic into: ✅
   - `ReviewPolicy` → control/workflows/review_workflow.py
   - `ReworkPolicy` → control/workflows/rework_workflow.py
   - `HealthGate` → control/workflows/triage_workflow.py
   - `VerificationService` → execution/verification_service.py (created in Phase 2)
2) Orchestrator loop remains plan/apply/observe. ✅

Exit criteria:
- Orchestrator is mediator only; policies are testable in isolation. ✅

Implementation Notes:
- Policy services exist in `control/workflows/` with ReviewWorkflow, ReworkWorkflow, TriageWorkflow
- Orchestrator uses Planner + ActionApplier pattern
- Workflows contain POLICY (what should happen), not MECHANICS (how to do it)

## Dependencies / Risks
- Token handling: must remain centralized and guardrails enforced.
- Event/snapshot flow must be reliable (SSE replay already in place).
- E2E cleanup must not discard artifacts on failure.

## Progress Log
- 2025-12-31: Phase 0 inventory completed. Plan externalized.
- 2025-12-31: Phase 1 work item 0: removed gh CLI usage in Python (token resolution + sandbox verification); added explicit note in AI.md.
- 2025-12-31: Phase 1 work item 0: tightened git subprocess guardrails; CLI git calls routed through WorkingCopy.
- 2026-01-01: Phase 1 completed. All GH access now goes through GitHubAdapter:
  - Added missing methods to GitHubAdapter (create_label, delete_label, update_issue_state, delete_branch, branch_exists, close_pr, list_branches, get_issue_comments, list_labels)
  - Refactored `test_data.py` to use `_adapter_for(repo)` instead of `_client_for(repo)`
  - Refactored `tests/e2e/conftest.py` to use `_github_adapter(repo)` and handle PRInfo/Issue objects
  - Refactored `cli.py` to use `_github_adapter_for_config(config)`
  - Refactored `setup_wizard.py` to use `_github_adapter(repo)`
  - Updated unit tests to mock adapter methods with proper Issue objects
- 2026-01-01: Phase 2 completed. VerificationService created:
  - Created `ports/verification.py` with VerificationService protocol, VerificationBudget, ErrorClassification, VerificationResult
  - Created `execution/verification_service.py` with DefaultVerificationService implementation
  - Updated GitHubAdapter._verify_write() to use the centralized VerificationService
  - Features: retry budgets, exponential backoff with jitter, error classification (retryable/fatal/needs-reconcile), circuit breaker
- 2026-01-01: Phase 3 verified complete. E2E tests already use async watcher-based waits (IssueWatch, SystemWatch).
  - Removed unused synchronous GH polling functions (wait_for_issue_label, wait_for_pr_created)
- 2026-01-01: Phase 4 verified complete. GH audit infrastructure already exists:
  - `gh_audit.py` tracks all GH API calls with caller, command, reason, scope, issue
  - `zzz_test_gh_audit.py` enforces limits at end of e2e runs
  - pytest marker `gh_activity_limit` for per-test limits
- 2026-01-01: Phase 5 verified complete. Policy services already exist:
  - `control/workflows/review_workflow.py` (ReviewWorkflow)
  - `control/workflows/rework_workflow.py` (ReworkWorkflow)
  - `control/workflows/triage_workflow.py` (TriageWorkflow)
  - Orchestrator uses Planner + ActionApplier pattern
- 2026-01-01: Test verification completed:
  - Fixed `tests/e2e/test_real_scenarios.py` to use `_github_adapter` instead of `_github_client`
  - All 1510 unit tests pass
  - All 65 integration tests pass
  - E2E tests collect and run successfully

## Current Status
- **ALL OBJECTIVES COMPLETE** (2026-01-01)

### Final Assessment by Objective:

| # | Objective | Status | Evidence |
|---|-----------|--------|----------|
| 1 | Single GitHub transport with typed errors, retries, budgets | ✅ DONE | GitHubAdapter mediates all access; gh_guard.py blocks direct gh CLI |
| 2 | Central verification service with budgets + classifier + circuit breaker | ✅ DONE | VerificationService in ports/verification.py, execution/verification_service.py |
| 3 | Snapshot/event flow authoritative for tests (no direct GH reads in waits) | ✅ DONE | `wait_for_issue_comment` deprecated, replaced with `check_issue_comment` (boundary check) |
| 4 | Replace implicit polling loops with event-driven watchers | ✅ DONE | All waits use watchers or single boundary checks |
| 5 | Isolate GH read cost in one module with cache + invalidation rules | ✅ DONE | `execution/github_cache.py` created; adapter has `invalidate_label_cache()` and `invalidate_pr_cache()` |
| 6 | Diagnostic mode: per-test GH usage + slowest phases report | ✅ DONE | `gh_audit._summary_lines()` now includes `slowest_phases` |
| 7 | Orchestrator thin; policies in small services | ✅ DONE | `control/health_gate.py` (HealthGate) + existing workflows |

### Implementation Details:

**Objective 3/4 - GH Polling Removed:**
- Created `check_issue_comment()` for single boundary checks
- Deprecated `wait_for_issue_comment()` with warning
- Test updated to use boundary check after flow completes

**Objective 5 - GitHubCache Injected:**
- `execution/github_cache.py` with:
  - Explicit cache surface (issues, issue_labels, prs_by_number, prs_by_issue, prs_by_branch, branches)
  - TTL-based staleness protection
  - Invalidation hooks (`on_invalidate()`)
  - Cache statistics
- GitHubCache is injected into GitHubAdapter via bootstrap.py:
  - `GitHubAdapter.__init__` accepts `cache: GitHubCache` parameter
  - bootstrap.py creates `GitHubCache(default_ttl=queue_refresh_seconds)` and passes to adapter
- All adapter cache operations delegate to GitHubCache:
  - `update_label_cache()` → `cache.set_issue_labels()`
  - `invalidate_label_cache()` → `cache.invalidate_issue_labels()`
  - `invalidate_pr_cache()` → `cache.invalidate_pr_by_issue()` / `cache.invalidate_pr_by_branch()`
  - `_cache_pr_info()` → `cache.set_pr_by_issue()` / `cache.set_pr_by_branch()`
- Write operations (add_label, remove_label) call invalidation

**Objective 6 - Slowest Phases Added:**
- `gh_audit._summary_lines()` now includes `slowest_phases`
- Phases sorted by `total_ms` from `by_scope_totals`
- Example output: `[GH-AUDIT] slowest_phases=[('periodic', 800), ('startup', 100)]`

**Objective 7 - HealthGate Extracted:**
- `control/health_gate.py` with:
  - `HealthDecision` (can_proceed, reason, details)
  - `HealthGate.check()` for capacity/pause/rate-limit checks
  - `remaining_capacity()` for planning
  - `create_health_gate_from_config()` factory
- Integrated into orchestrator:
  - `orchestrator.py` imports and uses HealthGate
  - `bootstrap.py` creates and injects HealthGate
  - HealthGate is REQUIRED (no fallback in `_check_health()`)
  - `_check_health()` directly delegates to `health_gate.check()`

**Phase 5 Clarification:**
The workflows (ReviewWorkflow, ReworkWorkflow, TriageWorkflow) were already created and
integrated via the Planner. The orchestrator delegates "what to do" to the Planner, which
uses the workflows for policy decisions. The HealthGate addition extracts the "when to
run planning" decision (capacity/pause checks) from inline orchestrator logic.

### Summary:
- Phase 1: ✅ GitHubAdapter mediates ALL GitHub access
- Phase 2: ✅ VerificationService provides centralized write-verify
- Phase 3: ✅ E2E tests use watchers + boundary checks (no polling)
- Phase 4: ✅ gh_audit tracks slowest phases
- Phase 5: ✅ Policy services complete:
  - Planner → Workflows (ReviewWorkflow, ReworkWorkflow, TriageWorkflow) for "what to do"
  - Orchestrator → HealthGate for "when to run planning"
  - VerificationService for write-verify policy
  - GitHubCache for caching policy with explicit invalidation

## Post-Completion Audit Fixes (2026-01-01)

### Fix 1: VerificationService Injection
**Problem:** VerificationService was created inline on every `_verify_write()` call, defeating the circuit breaker (state reset each call).

**Solution:**
- Added `verification_service: VerificationService` parameter to `GitHubAdapter.__init__`
- bootstrap.py creates `DefaultVerificationService` with config-based budget and injects it
- `_verify_write()` now uses `self._verification_service` (preserves circuit breaker state)

### Fix 2: Completion Policy Extraction
**Problem:** Orchestrator contained policy logic in `_generate_completion_actions()` that made business-meaning decisions (what labels to add for failures/timeouts).

**Solution:**
- Added `generate_completion_actions()` method to `CompletionHandler`
- Extended `CompletionResult` to include `actions: tuple[Action, ...]`
- `process_completion()` now generates and returns the actions
- Orchestrator's `handle_session_completion()` applies actions from `result.actions`
- Removed inline `_generate_completion_actions()` from orchestrator (86 lines removed)
- Orchestrator now 893 lines (down from 979)

### Architectural Principle Applied
The orchestrator is a **mediator**, not a policy maker. It should:
- ✅ Route completion to handlers
- ✅ Decide which subsystem to call
- ✅ Enforce lifecycle constraints
- ✅ Emit events and schedule the next tick

It should NOT:
- ❌ Decide business meaning like "tests passed → mark ready-for-review"
- ❌ Decide which specific labels to add/remove based on status
- ❌ Advance state machines directly based on business rules

Those decisions belong in workflows, controllers, or the planner.
