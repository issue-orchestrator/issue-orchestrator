# ADR 0015: Log enough that problem resolution is trivial

**Status:** Accepted
**Date:** 2024-12-21

## Context

When problems occur in production:
- Users report symptoms, not causes
- Reproducing issues locally is often impossible
- Time pressure makes thorough investigation difficult

Under-logging leads to:
- "Works on my machine" dead ends
- Guessing at root causes
- Multiple deploy cycles to add missing logs

Over-logging leads to:
- Log noise obscuring real issues
- Storage costs
- Performance impact

## Decision

**Log at the level where problem resolution becomes trivial.**

### What to Log

1. **State transitions** - Before and after state for any transition
   ```
   [STATE] issue #123: ready → in-progress (reason: session_started)
   ```

2. **External calls** - API requests with response status
   ```
   [GITHUB] GET /repos/owner/repo/issues/123 → 200 (etag: "abc123")
   ```

3. **Decision points** - Why a path was taken
   ```
   [DECISION] Skipping issue #123: blocked by #122 (not closed)
   ```

4. **Errors with context** - Full context, not just the exception
   ```
   [ERROR] Failed to apply label: issue=#123, label=in-progress,
           current_labels=[blocked], error=rate_limited
   ```

5. **Session lifecycle** - Start, completion, timeout
   ```
   [SESSION] Started: issue=#123, worktree=/tmp/wt-123, agent=claude
   [SESSION] Completed: issue=#123, outcome=completed, duration=5m32s
   ```

### What NOT to Log

- Loop iterations without state changes
- Successful cache hits (unless debugging cache)
- Internal data structure contents (use events for structured data)

### Log Levels

| Level | Use For |
|-------|---------|
| ERROR | Failures requiring attention |
| WARNING | Unexpected but handled conditions |
| INFO | State transitions, decisions, lifecycle |
| DEBUG | Detailed flow for development |

## Consequences

### Positive
- Production issues diagnosed from logs alone
- No "add logging and redeploy" cycles
- Clear audit trail for security review
- Onboarding developers can follow the flow

### Negative
- More disk usage
- Must filter noise when reading logs
- Sensitive data must be scrubbed

## Implementation

- Structured logging with consistent prefixes
- Request IDs for correlation across async operations
- Log rotation to manage disk usage
- Events (EventSink) complement logs for machine consumption
