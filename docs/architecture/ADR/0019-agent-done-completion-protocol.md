# ADR 0019: Structured completion protocol (agent-done)

**Status:** Accepted
**Date:** 2024-12-21

## Context

When an agent finishes work, the orchestrator needs to know:
- Did it succeed, fail, or get blocked?
- What actions does it want (create PR, add label)?
- What was implemented? What problems occurred?
- What ancillary work was discovered but intentionally deferred?

Options considered:
1. **Exit codes** - Too limited (success/fail only)
2. **Parse terminal output** - Fragile, unstructured
3. **Agent calls API directly** - Security risk (ADR-0016)
4. **Structured completion file** - Agent writes, orchestrator reads

## Decision

**Agents signal completion by writing a structured JSON file via the `agent-done` command.**

### The `agent-done` Command

```bash
# Success - work completed
agent-done completed \
  --implementation "Added user authentication with JWT" \
  --problems "None"

# Success - work completed, with ancillary follow-up proposals already written to a file
# Add --follow-up-file <existing-path> to the completed command above.

# Blocked - can't proceed
agent-done blocked \
  --reason "Depends on issue #122 which isn't merged" \
  --attempted "Tried to import auth module"

# Review outcomes
agent-done approved --summary "Code looks good, tests pass"
agent-done changes_requested --issues "Missing error handling in login.py"
```

### Completion Record Format

```json
{
  "schema_version": "1.0",
  "session_id": "issue-123-abc",
  "outcome": "completed",
  "requested_actions": ["create_pr", "add_label:ready-for-review"],
  "implementation": "Added JWT authentication",
  "problems": "None",
  "follow_up_issues": [
    {
      "title": "Isolate env-sensitive logging test",
      "reason": "Discovered while validating the assigned issue, but unrelated to the core fix",
      "suggested_labels": ["bug", "tests"],
      "blocking": false
    }
  ],
  "timestamp": "2024-12-21T10:30:00Z"
}
```

`follow_up_issues` are advisory only. Agents do not create GitHub issues directly; they report ancillary work and the orchestrator decides how to persist or surface it.

### Validation Runs First (Fast Feedback)

Before writing the completion record, `agent-done` runs the validation gate:

```
agent-done completed
         │
         ▼
    Run validation (tests, linting, type checks)
         │
    ┌────┴────┐
    │         │
  PASS      FAIL
    │         │
    ▼         ▼
Write      Print errors
completion  Agent can fix
record      and retry
```

**Why validate in agent-done:**
- **Fast feedback**: Agent sees failures immediately, can fix and retry
- **No wasted cycles**: Don't signal "completed" if tests fail
- **Clear contract**: Completion means "validated and ready"
- **Agent learns**: Repeated failures teach the agent what's expected

If validation fails, `agent-done` exits non-zero and the agent can:
1. Read the error output
2. Fix the issue
3. Run `agent-done` again

### Why a Command (not direct file write)

1. **Runs validation** - Tests/linting before completion (see above)
2. **Validates input** - Catches malformed completions early
3. **Consistent format** - Schema enforced at write time
4. **Audit trail** - Command logged in session history
5. **Extensible** - Add fields without breaking agents
6. **Discoverability** - `agent-done --help` shows options

### Orchestrator Processing

```
Agent writes completion.json
         │
         ▼
Orchestrator observes file (FactGatherer)
         │
         ▼
Validates as untrusted input
         │
         ▼
Planner decides actions based on outcome
         │
         ▼
ActionApplier executes (push, create PR, labels)
```

## Consequences

### Positive
- **Structured**: Machine-readable, schema-validated
- **Auditable**: Clear record of what agent reported
- **Secure**: Agent reports intent, orchestrator executes
- **Extensible**: New outcomes/fields without breaking changes

### Negative
- Agents must learn `agent-done` command
- Extra step vs implicit completion
- File-based IPC has latency

## Validation

Completion records are **untrusted input**:
- Validate schema version
- Sanitize string fields
- Verify session_id matches expected
- Reject unknown outcomes

## Related

- ADR-0016: Orchestrator as mediator
- `AGENT_PROTOCOL.md`: Full protocol specification
- `entrypoints/cli_tools/agent_done.py`: Command implementation
