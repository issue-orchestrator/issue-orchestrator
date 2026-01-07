# Configuration

This file describes the user-facing configuration keys.

See `../design/validation_model.md` and `../design/security_isolation.md` for the underlying design intent.

## Agents

Define agents that work on issues. Each agent key must match a GitHub label.

```yaml
agents:
  "agent:backend":
    prompt: ".issue-orchestrator/prompts/backend.md"
    model: "sonnet"           # haiku, sonnet, or opus
    timeout_minutes: 45       # Max session duration
    initial_prompt: "..."     # Optional: customize the first message sent to Claude
```

### initial_prompt

The `initial_prompt` is the first message sent to Claude when a session starts. It's template-processed with these variables:

| Variable | Description | Available |
|----------|-------------|-----------|
| `{issue_number}` | GitHub issue number | All agents |
| `{issue_title}` | Issue title | All agents |
| `{prompt}` | Path to prompt file | All agents |
| `{worktree}` | Path to worktree | All agents |
| `{model}` | Model name | All agents |
| `{permission_mode}` | Permission mode | All agents |
| `{pr_number}` | PR number being reviewed | Review agents only |

**Default for work agents:**
```
Work on issue #{issue_number}: {issue_title}. Follow the instructions in {prompt}. When done, use agent-done to report completion.
```

**Example custom initial_prompt:**
```yaml
agents:
  "agent:reviewer":
    prompt: ".issue-orchestrator/prompts/reviewer.md"
    model: "sonnet"
    initial_prompt: "Review PR #{pr_number} for issue #{issue_number}: {issue_title}. Follow {prompt}. Use agent-done when done."
```

Note: The prompt file itself is NOT template-processed. Put static instructions in the prompt file; dynamic context goes in `initial_prompt`.

## Validation (publish gate)

```yaml
validation:
  publish_gate:
    cmd: "make validate"
    timeout_seconds: 1800

validation_policy:
  publish_requires: "publish_gate"
```

## Optional fast feedback for agents

```yaml
validation:
  agent_gate:
    cmd: "make validate-fast"
    timeout_seconds: 600
  publish_gate:
    cmd: "make validate"
    timeout_seconds: 1800

validation_policy:
  agent_runs: "agent_gate"
  publish_requires: "publish_gate"
```

## Isolation

```yaml
isolation:
  mode: "standard"   # or "hardened"
```
