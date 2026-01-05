# ADR 0002: Treat writes as untrusted until observed (write → verify loop)

**Status:** Accepted  
**Date:** 2025-12-31

## Context
GitHub exhibits eventual consistency and distributed replication effects. A successful API write (e.g., add label, create PR) does **not** guarantee that an immediate subsequent read (or a different endpoint) will reflect the change.

The orchestrator’s correctness depends on external state (GitHub issues/labels/PRs) being observed reliably. Trusting write responses leads to flakiness, hidden drift, and hard-to-debug reconciliation gaps.

## Decision
Adopt a uniform correctness rule:

> All external writes are considered **tentative** until confirmed by subsequent observation.

For any “write” operation that the orchestrator depends on:
1. Perform the write (POST/PATCH/PUT/DELETE).
2. Poll using **GET** to observe the expected change.
3. Use **conditional GET** (`If-None-Match` / ETags) to keep polling cheap.
4. If observation does not converge within bounded retries/time, mark the issue as requiring reconciliation and pause/fail-closed.

We explicitly do **not** rely on write responses returning ETags or providing read-your-writes semantics.

## Consequences
### Positive
- Predictable correctness under eventual consistency.
- Drift detection becomes systematic instead of ad-hoc.
- E2E tests become more reliable (they can wait for observed states).

### Negative / Costs
- Slightly more API calls in “write paths” (mitigated by ETag polling).
- Need a small shared implementation for write→observe loops (to avoid duplication).

## Alternatives considered
- Trust write success and proceed: rejected due to consistency/race failures.
- Use conditional writes with `If-Match`: rejected as not universally supported/robust for our needs.
- Replace GitHub as the source of truth with a DB: deferred as future enhancement.

## Follow-ups
- Provide a single adapter primitive: `write_then_observe(write_op, observe_get, predicate, budget)`.
- Emit events for “write pending”, “write observed”, and “write stalled”.
- Ensure reconciliation logic can repair/handle stalled writes safely.
