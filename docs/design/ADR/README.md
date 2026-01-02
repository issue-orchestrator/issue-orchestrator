# Architectural Decision Records (ADRs)

These ADRs capture the *few* architectural decisions that materially affect correctness, security, boundaries, and extensibility.

**Rules**
- ADRs are append-only: do **not** rewrite history. If a decision changes, add a new ADR that **supersedes** an older one.
- Keep ADRs short (aim for ~1 page).
- Prefer decisions that prevent architectural drift over “nice-to-have” notes.

## Index

- [0001 Use a single GitHub HTTP client (httpx sync) and avoid gh/ghapi in runtime](0001-single-github-http-client-httpx-sync.md)
- [0002 Treat writes as untrusted until observed (write → verify loop)](0002-write-then-observe.md)
- [0003 Model inbound truth as Observations (not mixed facts/decisions)](0003-observations-as-inbound-truth.md)
- [0004 Centralize reconciliation (startup + runtime) behind a single entrypoint](0004-centralize-reconciliation.md)
- [0005 Enforce human merge and agent credential isolation](0005-human-merge-and-agent-credential-isolation.md)
- [0006 Cache external reads with explicit refresh policy + ETags](0006-caching-and-refresh-policy.md)
