# ADR 0010: Verification failure policy (retry → pause → quarantine)
**Status:** Proposed / Recommended  
**Date:** 2025-12-31

## Context
GitHub reads are eventually consistent, and GitHub availability/network conditions vary. The orchestrator must not advance correctness-critical state unless it can confirm external truth, but it should not become unusable due to transient failures.

## Decision
Adopt a single failure handling policy for verification:
- retry with bounded backoff for transient failures
- if systemic failure persists, enter a global PAUSED state (circuit breaker), continue health probing, auto-resume
- if issue-local mismatch is detected, mark issue `needs-reconcile` and continue other work
- verification is always-on; only budgets are configurable

## Consequences
- correctness is preserved under eventual consistency
- reduced flakiness in e2e tests
- operational clarity: paused state is visible, recoverable, and auditable
