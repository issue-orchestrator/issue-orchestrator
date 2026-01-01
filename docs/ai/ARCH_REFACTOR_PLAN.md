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

### Phase 1: Single GitHub Transport (IN PROGRESS)
Goal: Eliminate direct `GitHubHttpClient` usage outside the adapter layer.

Deliverables:
- New GH service interface (ports) used by prod + test_data + e2e.
- `test_data.py` uses adapter/service instead of direct HTTP client.
- E2E GH helpers use adapter/service (not raw HTTP client).
- Typed errors unified: `GitHubHttpError` remains canonical.

Work items:
- Introduce `GitHubService` (or expand adapter) as the only GH entry point.
- Route `test_data.py` through adapter/service.
- Route `tests/e2e/conftest.py` GH helpers through adapter/service.
- Ensure gh_audit continues to annotate calls.

Exit criteria:
- No direct `GitHubHttpClient` usage outside `execution/`.
- Tests still pass (unit/integration).

### Phase 2: Central Verification Service
Goal: One place for write-verify + retry + budget policy.

Deliverables:
- `VerificationService` with:
  - retry budgets (time, attempts)
  - classifier (retryable vs fatal)
  - circuit breaker (pause vs needs-reconcile)
- Adapter/service delegates all verify loops to it.

Exit criteria:
- All write verification uses `VerificationService`.
- Logging is consistent and includes last observed state.

### Phase 3: Authoritative Event/Snapshot Tests
Goal: Tests rely on orchestrator event flow, not GH polling.

Deliverables:
- Replace GH polling waits in e2e with watcher/snapshot waits.
- Explicit `trigger_refresh()` boundaries after writes.
- Direct GH reads only for setup/cleanup.

Exit criteria:
- No GH read in test wait loops.
- Event/snapshot waits are the only mechanism for assertions.

### Phase 4: GH Cost Isolation + Diagnostics
Goal: Make GH usage visible and enforceable.

Deliverables:
- `GitHubCost` module (small cache + invalidation rules).
- Diagnostic mode: per-test GH usage + slowest phases report.
- CI/local gating via gh-activity limits.

Exit criteria:
- GH usage report emitted per e2e run.
- Limits enforced consistently.

### Phase 5: Thin Orchestrator + Policy Services
Goal: Orchestrator delegates policy logic to small services.

Deliverables:
- Move remaining policy logic into:
  - `ReviewPolicy`
  - `ReworkPolicy`
  - `HealthGate`
  - `VerificationService`
- Orchestrator loop remains plan/apply/observe.

Exit criteria:
- Orchestrator is mediator only; policies are testable in isolation.

## Dependencies / Risks
- Token handling: must remain centralized and guardrails enforced.
- Event/snapshot flow must be reliable (SSE replay already in place).
- E2E cleanup must not discard artifacts on failure.

## Progress Log
- 2025-12-31: Phase 0 inventory completed. Plan externalized.

