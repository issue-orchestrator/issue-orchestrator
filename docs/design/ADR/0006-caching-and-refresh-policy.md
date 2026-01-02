# ADR 0006: Cache external reads with explicit refresh policy + ETags

**Status:** Accepted  
**Date:** 2025-12-31

## Context
The orchestrator reads GitHub frequently to:
- discover candidate issues / sessions
- reconcile state transitions against external truth
- verify writes (labels, comments, PR operations)
- support WebUI/test watchers

GitHub is rate-limited and occasionally slow. We have already implemented caches for issues and PRs, with refresh parameters configured in YAML, to reduce load. We also want conditional requests (ETags) to make polling cheap when nothing changes.

However, caching can undermine correctness if:
- core assumes cache is “truth”
- refresh policy is implicit or scattered
- write→verify loops reuse stale cached data
- multi-step workflows rely on cache across boundaries

## Decision
Adopt an explicit caching contract for external reads:

1. **Caching lives in adapters**, not in core.
   - Core consumes Observations/Snapshots.
   - Adapter may serve cached snapshots *only* under an explicit refresh policy.

2. **Every cached dataset has a refresh policy** configured (YAML):
   - `ttl_seconds` (max age before refresh is required)
   - `force_refresh_on` triggers (startup, before_apply, after_apply, on_completion, manual_refresh)
   - optional `max_stale_seconds` for “best-effort” views (e.g., UI), but **not** for correctness-critical paths.

3. **Correctness-critical paths bypass stale cache**:
   - `reconcile()` and write→verify loops must request **fresh** observations (or conditional GET using stored ETag).
   - After writes, verification must be based on observed state, not cached state.

4. **Use ETags for conditional GET where applicable**:
   - Cache stores `{etag, payload, fetched_at}` keyed by endpoint+params.
   - Refresh performs GET with `If-None-Match`; `304` retains payload and updates `fetched_at` if desired.

5. **Separate “UI cache” vs “control cache” semantics**:
   - UI may tolerate staleness (configurable) with clear indicators.
   - Control plane must fail closed or pause when it cannot obtain fresh observations for correctness.

## Consequences
### Positive
- Fewer GitHub calls and lower flake rate under rate limits.
- Clear contract: caching improves performance without compromising correctness.
- ETags reduce polling costs drastically when state is stable.
- Better testability: adapter cache behavior can be mocked deterministically.

### Negative / Costs
- Requires discipline: callers must declare freshness requirements (e.g., `fresh=True`).
- Slightly more adapter complexity (cache store, keys, etag plumbing).

## Alternatives considered
- No caching, only ETags: rejected because some endpoints still require payload parse and repeated pagination.
- Cache everywhere by default: rejected due to correctness risks and drift masking.
- Replace GitHub with DB store: deferred as future option.

## Follow-ups
- Add a small `CacheKey` strategy: `(method, url, normalized_query)` to avoid collisions.
- Add tests:
  - cache hit within TTL returns cached payload
  - stale cache triggers conditional GET (304 retains payload)
  - correctness-critical caller requests fresh and forces refresh
  - write→verify loop does not accept cached state as verification
- Add WebUI indicators for “stale snapshot” if serving best-effort views.
