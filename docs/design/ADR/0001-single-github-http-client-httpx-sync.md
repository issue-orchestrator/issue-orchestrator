# ADR 0001: Use a single GitHub HTTP client (httpx sync) and avoid gh/ghapi in runtime

**Status:** Accepted  
**Date:** 2025-12-31

## Context
The issue-orchestrator interacts with GitHub frequently (polling, reconciliation, labels/comments, PR operations). We previously used the `gh` CLI in places and considered adding a higher-level Python client (e.g., ghapi) for setup/utility commands.

We observed:
- CLI invocations can hang and are harder to time-bound deterministically.
- Process-level tooling (`subprocess`) increases “escape hatches” for agents and makes isolation/guardrails harder.
- Introducing multiple GitHub client stacks increases complexity: multiple auth stories, multiple retry policies, multiple test harnesses, and additional “quarantine” enforcement.

## Decision
Use **one** GitHub client implementation throughout the project:
- **httpx** in **synchronous** mode (`httpx.Client`) as the HTTP transport.
- All GitHub interactions go through a single adapter layer (ports + adapters), not direct calls from control/domain code.
- Avoid `gh` CLI, and do not introduce `ghapi`/`PyGithub` for runtime paths.

Setup utilities (setup wizard, create-issue utility) must also reuse the same adapter/client.

## Consequences
### Positive
- Deterministic timeouts, retries, and error classification (no “hung process”).
- One auth configuration and one operational profile.
- Easier to enforce boundaries (no `subprocess`/CLI usage in core).
- Simpler tests (mock one transport; consistent fixtures).

### Negative / Costs
- We implement some thin wrappers ourselves (ETag caching, pagination helpers, etc.).
- We do not benefit from a high-level object model (PyGithub) or generated endpoint surface (ghapi).

## Alternatives considered
- **Keep `gh` CLI**: rejected due to hangs, process control complexity, and weaker guardrails.
- **Use ghapi** for setup-only: rejected because containment tax exceeds value for small surface area.
- **Use PyGithub**: rejected due to leaky/awkward HTTP semantics and ETag/polling needs.

## Follow-ups
- Implement a `GitHubHttpClient` wrapper (timeouts, retries/backoff, correlation ids).
- Implement conditional GET support (ETags) for polling-heavy reads.
- Add architectural guardrails to keep network/code-exec out of core, and GitHub code confined to adapters.
