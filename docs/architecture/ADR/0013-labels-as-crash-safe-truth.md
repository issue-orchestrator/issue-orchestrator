# ADR 0013: GitHub labels as crash-safe source of truth

**Status:** Accepted
**Date:** 2024-12-21

## Context

The orchestrator can crash, restart, or lose local state at any time. It needs to recover gracefully and continue processing without human intervention or data loss.

Options considered:
1. Local database as source of truth - fast but loses state on crash
2. GitHub labels as source of truth - survives crashes, single source
3. Hybrid with sync - complex reconciliation logic

## Decision

**GitHub labels are the authoritative source of truth for issue/PR state.**

### How It Works

1. **State encoded in labels**: `in-progress`, `needs-code-review`, `needs-rework`, etc.
2. **On startup**: Orchestrator reads labels to reconstruct state
3. **On crash**: No local state to recover - just re-read labels
4. **State transitions**: Apply label changes, verify via observation (ADR-0002)

### Label Semantics

| Label | Meaning |
|-------|---------|
| `in-progress` | Agent session active |
| `needs-code-review` | PR awaiting review |
| `code-reviewed` | Review passed, awaiting tech lead |
| `needs-rework` | Review requested changes |
| `needs-human` | Escalated, human intervention required |
| `tech-lead-needs-human` | Provenance marker for a `needs-human` escalation owned by the tech lead launch workflow; informational, not independently blocking |
| `blocked` | Dependencies not met |

The tech lead launch workflow writes `tech-lead-needs-human` before `needs-human`.
It only creates that ownership marker when both labels are absent, so it never
adopts a pre-existing human/session-owned `needs-human` transition. A targeted
marker-label read on every unpaused reconciliation tick recovers a marker-only
crash by restoring the blocking `needs-human` label, even when the in-memory
tech lead queue was lost.
When a running or restored investigation supersedes that escalation, the
orchestrator removes `needs-human` first and then its marker. A bare
`needs-human` label has no orchestrator-owned provenance and is never removed by
this reconciliation. This discovery and ordering make partial writes and
removals safe to retry from labels alone after a crash.

### Recovery Flow

```
Orchestrator starts
    │
    ▼
Fetch all issues with orchestrator labels
    │
    ▼
For each issue:
  - Read labels → determine state
  - Check for active session (tmux/iTerm2)
  - Resume or clean up as appropriate
    │
    ▼
Enter normal loop
```

## Consequences

### Positive
- **Crash-safe**: No local state to lose
- **Observable**: Humans can see state in GitHub UI
- **Recoverable**: Restart = automatic recovery
- **Single source**: No sync conflicts

### Negative
- API calls on every state check (mitigated by caching, ADR-0006)
- Label changes must be verified (ADR-0002, ADR-0007)
- Limited expressiveness (labels are flat strings)

## Related

- ADR-0002: Write-then-observe
- ADR-0006: Caching with ETags
- ADR-0007: External state reconciliation
