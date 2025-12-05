# Issue Orchestrator Agent Protocol v1.0

This document defines the contract between issue-orchestrator and AI agents.
Third-party agents should follow this protocol to work correctly with the orchestrator.

## Overview

The orchestrator launches agents in isolated environments to work on GitHub issues.
Agents communicate status back via GitHub issue comments and labels.

## Environment

When launched, agents receive:
- **Working directory**: An isolated git worktree with a branch for the issue
- **Branch name**: `{issue_number}-{slugified-title}`
- **Initial prompt**: Contains issue number, title, and path to detailed instructions

## Expected Behavior

### 1. Read Instructions
The agent's initial prompt references a prompt file (e.g., `prompts/simple-fix.md`).
Read and follow those instructions.

### 2. Do the Work
- Implement the fix or feature
- Write tests
- Commit changes
- Create a PR

### 3. Report Results

Post a comment on the issue using **structured headings**.

#### On Completion
```markdown
## Implementation
- What was implemented
- Key files changed

## Problems Encountered
- Any issues (or "None")

## Pull Request
- Link to the PR
```

#### If Blocked
Add the `blocked` label and post:
```markdown
## Blocked
- What was attempted
- Why it failed
- What's needed to proceed
```

#### If Human Input Needed
Add the `needs-human` label and post:
```markdown
## Needs Human Input
- Specific question
- Context for the decision
```

### 4. Exit
After posting the appropriate comment, exit cleanly.

## Labels

| Label | Meaning | Who Sets It |
|-------|---------|-------------|
| `in-progress` | Work is underway | Orchestrator (on launch) |
| `blocked` | Agent couldn't complete | Agent |
| `needs-human` | Agent needs clarification | Agent |

The orchestrator removes `in-progress` when it detects session completion.

## Detection Logic

The orchestrator determines session status by:
1. **Tmux window exists** → RUNNING
2. **Tmux window gone + PR exists** → COMPLETED
3. **Tmux window gone + `blocked` label** → BLOCKED
4. **Tmux window gone + `needs-human` label** → NEEDS_HUMAN
5. **Tmux window gone + no PR/labels** → FAILED
6. **Runtime > timeout** → TIMED_OUT

## Configuration

Agents are configured in `.issue-orchestrator.yaml`:

```yaml
agents:
  "agent:web":
    prompt: ".issue-orchestrator/prompts/web.md"
    worktree_base: "../"
    timeout_minutes: 45
    model: sonnet
    # Optional: custom command template
    command: "claude --model {model} '{initial_prompt}'"
```

## Comment Headings (Optional)

Projects can customize comment headings for tooling integration:

```yaml
comment_headings:
  implementation: "## Implementation"
  problems: "## Problems Encountered"
  pr_link: "## Pull Request"
  blocked: "## Blocked"
  needs_human: "## Needs Human Input"
```

## Best Practices

1. **Exit cleanly** - Don't hang or loop forever
2. **Be specific in comments** - Help humans understand what happened
3. **Use labels correctly** - Don't forget to add `blocked` or `needs-human`
4. **Create good PRs** - Include description, link to issue
5. **Respect timeouts** - Long-running tasks should checkpoint progress
