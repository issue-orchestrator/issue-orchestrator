# ADR 0020: Quick and publish validation gates

**Status:** Accepted
**Date:** 2026-05-08

## Context

Agents need validation at two different lifecycle points:

- Immediate feedback when `coding-done completed` is called, while the coding agent can still fix the worktree.
- A deeper, authoritative local gate before the orchestrator pushes or publishes code.

One command for both jobs creates bad pressure. If the command is deep enough
for publish, review back-and-forth is slow. If it is cheap enough for every
review turn, late pre-push failures surprise the agent after review approval.

## Decision

Validation has two configured commands:

```yaml
validation:
  quick:
    cmd: "make validate-quick"
    timeout_seconds: 300
  publish:
    cmd: "make validate-pr"
    timeout_seconds: 1800
    dirty_check: tracked
```

Errata: when the user-facing `make validate-pr` target wraps the cache-aware
`scripts/verify-pr.sh` path, `validation.publish.cmd` should instead point to
a private non-recursive suite command.

`validation.quick.cmd` runs on `coding-done completed` and inside local
coder/reviewer exchange. Repos should put fast tests, lint/type checks, and
cheap policy scans here, including project-specific bans on newly added test
skips such as `assumeTrue`, `assumeFalse`, `@Disabled`, or `@Ignore`.

`validation.publish.cmd` runs through the repo pre-push/pre-publish path and is
the authoritative local branch-readiness gate. Its dirty-tree policy lives at
`validation.publish.dirty_check`.

## Consequences

### Positive

- Agents get earlier feedback for common correctness and policy failures.
- Review loops can stay fast without weakening the publish gate.
- Repos own their exact validation commands instead of hard-coded orchestrator scans.
- Publish validation can remain as deep as the repo needs.

### Negative

- Repos must maintain two commands when their fast and publish checks differ.
- A cheap quick command can still miss failures caught by the publish gate.

## Related

- `docs/architecture/validation.md`: Validation system design
- `docs/design/guardrails.md`: Guardrail pipeline
