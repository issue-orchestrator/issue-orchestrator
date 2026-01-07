# Issue Orchestrator Agent Protocol v2.0

This document defines the contract between issue-orchestrator and AI agents.

## Who This Is For

**Prompt authors** writing agent prompts for their repo. Agents don't read this file directly - instead, include the relevant `agent-done` commands in your prompt files (see `examples/prompts/` for templates).

When setting up issue-orchestrator for a new repo, use this as a reference for writing your `.issue-orchestrator/prompts/*.md` files.

## Core Principle

**Agents report intent; the orchestrator executes.**

Agents do NOT:
- Push code
- Create PRs
- Post GitHub comments
- Mutate labels
- Touch GitHub in any way

The orchestrator handles all external system interactions after validating agent output as untrusted input. See [ADR-0016](docs/architecture/ADR/0016-orchestrator-as-mediator.md).

## Environment

When launched, agents receive:

| Variable | Description |
|----------|-------------|
| `ORCHESTRATOR_SESSION_ID` | Unique session identifier |
| `ORCHESTRATOR_COMPLETION_PATH` | Where to write completion record |
| Working directory | Isolated git worktree with issue branch |

The branch name follows: `{issue_number}-{slugified-title}`

## The `agent-done` Command

Agents signal completion using the `agent-done` CLI command. This is the ONLY sanctioned way to complete work.

### Completion Statuses

```bash
# Work completed successfully
agent-done completed \
  --implementation "Added JWT authentication to login endpoint" \
  --problems "None"

# Blocked - cannot proceed
agent-done blocked \
  --reason "Depends on auth service not yet deployed" \
  --attempted "Tried to call auth endpoint, got 404"

# Need human decision
agent-done needs_human \
  --question "Should we use OAuth or API keys?" \
  --options "OAuth" "API keys" \
  --default "OAuth after 24h"

# Code review: approved
agent-done approved \
  --summary "Clean implementation, tests pass" \
  --risk low

# Code review: changes requested
agent-done changes_requested \
  --issues "Missing error handling in auth.py:45" \
  --risk medium
```

### Required Fields by Status

| Status | Required Fields |
|--------|-----------------|
| `completed` | `--implementation`, `--problems` |
| `blocked` | `--reason`, `--attempted` |
| `needs_human` | `--question` |
| `approved` | `--summary`, `--risk` |
| `changes_requested` | `--issues`, `--risk` |

### What Happens After `agent-done`

1. Validation runs (if configured) - tests, linting, type checks
2. If validation fails, agent-done exits non-zero - agent can fix and retry
3. If validation passes, completion record is written to `.issue-orchestrator/completion.json`
4. Orchestrator detects the file and processes it
5. Orchestrator executes requested actions (push, create PR, add labels, post comment)

## Validation

Before writing the completion record, `agent-done` runs the configured validation gate:

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
Write     Exit non-zero
record    Agent fixes and retries
```

This gives agents fast feedback. See [ADR-0019](docs/architecture/ADR/0019-agent-done-completion-protocol.md).

## Completion Record Format

The `agent-done` command writes a JSON file:

```json
{
  "session_id": "issue-123-abc",
  "timestamp": "2024-12-21T10:30:00Z",
  "outcome": "completed",
  "summary": "Completed: Added JWT authentication...",
  "requested_actions": ["push_branch", "create_pr", "post_comment"],
  "implementation": "Added JWT authentication to login endpoint",
  "problems": "None",
  "comment_body": "## Implementation\n\nAdded JWT authentication..."
}
```

### Outcomes

| Outcome | Meaning | Requested Actions |
|---------|---------|-------------------|
| `completed` | Work done, ready for PR | push, create_pr, post_comment |
| `blocked` | External blocker | push, add_blocked_label, post_comment |
| `needs_human` | Need clarification | push, add_needs_human_label, post_comment |
| `review_approved` | Code review passed | add_code_reviewed_label, post_comment |
| `review_changes_requested` | Needs fixes | add_needs_rework_label, post_comment |

## Session Lifecycle

```
Orchestrator claims issue
       │
       ▼
Creates worktree + branch
       │
       ▼
Launches agent in terminal (tmux/iTerm2)
       │
       ▼
Agent works...
       │
       ▼
Agent runs: agent-done <status> ...
       │
       ▼
Orchestrator detects completion.json
       │
       ▼
Orchestrator validates record
       │
       ▼
Orchestrator executes actions (push, PR, labels, comment)
       │
       ▼
Session complete
```

## Labels

All labels are managed by the orchestrator:

| Label | Meaning | Set By |
|-------|---------|--------|
| `in-progress` | Work underway | Orchestrator (on launch) |
| `blocked` | Agent reported blocked | Orchestrator (from completion record) |
| `needs-human` | Agent needs clarification | Orchestrator (from completion record) |
| `pr-pending` | PR created, awaiting merge | Orchestrator (after PR creation) |
| `needs-code-review` | PR ready for review | Orchestrator (configurable) |
| `needs-rework` | Reviewer requested changes | Orchestrator (from review completion) |

## Configuration

Agents are configured in `.issue-orchestrator/config/<name>.yaml`:

```yaml
agents:
  agent:backend:
    prompt: ".issue-orchestrator/prompts/backend.md"
    model: sonnet
    timeout_minutes: 45
    worktree_base: "../"

  agent:reviewer:
    prompt: ".issue-orchestrator/prompts/reviewer.md"
    model: sonnet
    timeout_minutes: 30

validation:
  agent_gate:
    cmd: "make validate-fast"
    timeout_seconds: 300
  publish_gate:
    cmd: "make validate"
    timeout_seconds: 1800

review:
  enabled: true
  default: agent:reviewer
  code_review_label: needs-code-review
  code_reviewed_label: code-reviewed
  max_rework_cycles: 3
```

## Best Practices

1. **Always use `agent-done`** - Don't exit without calling it
2. **Be specific** - Detailed `--implementation` and `--problems` help humans
3. **Fix validation failures** - If agent-done fails, fix and retry
4. **Respect timeouts** - Long tasks should complete before timeout
5. **Don't touch GitHub** - The orchestrator handles all external operations
