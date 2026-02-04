# ADR 0020: Single validation command (e2e excluded for speed)

**Status:** Accepted
**Date:** 2024-12-21

## Context

Agents run validation frequently:
- Before every `agent-done completed`
- Multiple times per session if fixes needed
- Across many concurrent sessions

Full test suites (including e2e) can take minutes. Slow validation means:
- Agents wait idle
- Feedback loops slow down
- Sessions time out

## Decision

**Validation is a single user-defined command. E2e tests excluded by default for speed.**

### Single Validation Gate
```yaml
validation:
  script: "repo-guardrails/validate/run.sh"
  args: []
  env: {}
  timeout_seconds: 300
```

Legacy configs used `validation.agent_gate`/`validation.publish_gate`; those keys are removed in favor of `validation.script` (or legacy `validation.cmd`).

### What the Single Gate Runs

| Gate | Includes | Excludes | When |
|------|----------|----------|------|
| `validation.script` | Unit tests, linting, type checks | E2e, integration | Every `agent-done` and pre-push |

### Why Single Command (not orchestrator-managed)

1. **Project knows best** - Each repo defines its own validation
2. **No reimplementation** - Don't rebuild pytest/jest/make
3. **Flexible** - Can run anything (docker, remote CI, etc.)
4. **Cacheable** - Results cached by commit SHA

### E2e Test Timing (Unresolved)

When should e2e tests run? Options:

| Option | Pros | Cons |
|--------|------|------|
| **In validation.script** | Catches issues before merge | Slow, blocks PR |
| **Post-merge CI** | Fast PR flow | Issues found late |
| **Nightly/scheduled** | No PR blocking | Very late feedback |
| **On-demand** | Explicit control | May be forgotten |

**Current stance:** E2e in CI (server-side), not in local validation gates. The orchestrator observes CI status rather than running e2e locally.

## Consequences

### Positive
- **Fast feedback**: Validation command completes in seconds
- **Flexible**: Projects define their own commands
- **Cached**: Same SHA = skip validation

### Negative
- E2e gaps may slip through local validation
- Cache invalidation complexity

## Related

- ADR-0002: Write-then-observe (CI observation)
- `docs/architecture/validation.md`: Validation system design
