# AI Guide: GitHub Caching + ETags

Purpose: prevent stale reads and cache regressions when touching GitHub IO.

## Golden Rules

1) **Use the adapter, not the raw client.**
   - Use `GitHubAdapter` methods (`add_label`, `remove_label`, `get_issue_labels`, etc.).
   - Avoid calling `GitHubHttpClient` directly in orchestrator logic.

2) **Treat writes as eventually consistent.**
   - After a write, verification must use a **fresh read** (`use_cache=False`).
   - Never rely on 304/ETag for correctness‑critical checks after a mutation.

3) **Invalidate caches after writes.**
   - Adapter methods already invalidate issue label caches and ETag entries.
   - If you add new write paths, add explicit invalidation and tests.

4) **Differentiate read intent.**
   - **Correctness‑critical read:** use `get_issue_labels_fresh`.
   - **Routine read:** use `get_issue_labels` (ETag caching OK).

5) **Test what you change.**
   - Unit tests should assert fresh read usage and cache invalidation.
   - Live e2e should verify add/change/remove label behavior against GitHub.

## Where to Look

- Human doc: `docs/development/CACHING_ETAGS.md`
- Implementation: `src/issue_orchestrator/adapters/github/github_adapter.py`
- ETag logic: `src/issue_orchestrator/adapters/github/http_client.py`
