# Caching + ETags: Practical Guide

This project uses ETag caching to reduce GitHub API calls, but correctness depends
on knowing when to bypass cache and when to invalidate it.

## Mental Model

- **ETag caching** saves bandwidth for read‑only calls. It is *not* a freshness guarantee.
- **Writes are eventually consistent.** After a mutation, the API may briefly return stale data.

## Do / Don’t

**Do**
- Use `GitHubAdapter` as the boundary for all GitHub IO.
- After any write, verify with a **fresh read** (`use_cache=False`).
- Invalidate relevant caches after writes (issue label cache + ETag entries).
- Keep “correctness‑critical” reads explicit (`get_issue_labels_fresh`).
- Add tests whenever you introduce new write paths.

**Don’t**
- Don’t use cached reads to verify a write.
- Don’t call `GitHubHttpClient` directly from orchestration logic.
- Don’t assume 304 means “fully up‑to‑date.”

## Common Scenarios

### Add/Remove/Change Label

1. Perform write (`add_label`, `remove_label`).
2. Verify with fresh read (`get_issue_labels_fresh`).
3. Invalidate caches after the write (adapter does this).

### Read‑Only Polling

Use cached reads for background/status checks when correctness is not critical.

## Tests That Guard This

- Unit:
  - `tests/unit/test_github_adapter.py` covers cache invalidation + fresh‑read verification.
  - `tests/unit/test_github_http.py` covers ETag behavior.
- Live E2E:
  - `tests/e2e/test_label_write_verification.py` validates add/change/remove on real GitHub.

## If You Add a New Write Path

1. Add the write in `GitHubAdapter` (not raw client).
2. Add a fresh‑read verification.
3. Invalidate caches.
4. Add a unit test.
