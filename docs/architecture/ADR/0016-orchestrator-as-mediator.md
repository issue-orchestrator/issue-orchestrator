# ADR 0016: Orchestrator as mediator (agents never touch GitHub)

**Status:** Accepted
**Date:** 2024-12-21

## Context

Agents need to:
- Push code to branches
- Create pull requests
- Report completion status

If agents have direct GitHub access:
- They can bypass guardrails (merge PRs, skip checks)
- Credentials can leak to untrusted code
- No audit trail of what agents actually did
- Race conditions between agent and orchestrator

## Decision

**The orchestrator mediates all external system access. Agents work locally and signal intent; the orchestrator executes.**

### Agent Boundaries

Agents CAN:
- Read/write files in their worktree
- Run local commands (build, test, lint)
- Commit to their local branch
- Write completion records (JSON files)

Agents CANNOT:
- Push to remote (no credentials)
- Create PRs (no `gh` auth)
- Modify GitHub labels/state
- Access other worktrees

### Orchestrator Responsibilities

1. **Creates worktrees** with isolated environments
2. **Launches agent sessions** in sandboxed terminals
3. **Observes completion** via JSON files (not API calls from agent)
4. **Pushes code** on agent's behalf after validation
5. **Creates PRs** with proper metadata
6. **Applies labels** based on state machine transitions

### Communication Protocol

```
Agent writes:     .issue-orchestrator/completion.json
                  {
                    "outcome": "completed",
                    "requested_actions": ["create_pr"],
                    "implementation": "Added feature X"
                  }

Orchestrator:     1. Reads completion.json
                  2. Validates (untrusted input!)
                  3. Runs validation gate
                  4. Pushes branch
                  5. Creates PR
                  6. Updates labels
```

## Consequences

### Positive
- **Secure**: Agents can't bypass guardrails
- **Auditable**: All GitHub writes go through orchestrator
- **Controllable**: Orchestrator can pause, retry, or reject
- **Testable**: Agent behavior tested without GitHub access

### Negative
- Agents can't self-serve (must wait for orchestrator)
- Completion protocol adds complexity
- Orchestrator is a bottleneck for external operations

## Related

- ADR-0005: Human merge and agent credential isolation
- ADR-0012: Mechanical guardrails
- `AGENT_PROTOCOL.md`: Completion record format
