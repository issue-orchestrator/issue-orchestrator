# issue-orchestrator

Orchestrate AI agents working on GitHub issues in parallel.

## What it does

- Pulls issues from GitHub based on labels (e.g., `agent:web`, `agent:mobile`)
- Launches Claude Code (or other AI) sessions in isolated git worktrees
- Monitors sessions for completion, blocked state, or need for human input
- Manages concurrency (N parallel sessions)
- Provides a dashboard to monitor progress and attach to sessions

## Installation

```bash
pip install issue-orchestrator
```

Or from source:
```bash
git clone https://github.com/yourname/issue-orchestrator
cd issue-orchestrator
pip install -e ".[dev]"
```

## Prerequisites

- Python 3.11+
- `gh` CLI (authenticated)
- `tmux`
- `git`
- `claude` CLI (Claude Code)

## Quick Start

1. Create a config file in your repo:

```yaml
# .issue-orchestrator.yaml
agents:
  "agent:web":
    prompt: ".issue-orchestrator/prompts/web.md"
    worktree_base: "../"

  "agent:mobile":
    prompt: ".issue-orchestrator/prompts/mobile.md"
    worktree_base: "../"

concurrency:
  max_sessions: 3
  session_timeout_minutes: 45

# Optional: only process issues with this label (useful for testing)
# filter_label: test-data
```

2. Run the orchestrator:

```bash
issue-orchestrator start
```

## Commands

| Command | Description |
|---------|-------------|
| `issue-orchestrator start` | Start the orchestrator |
| `issue-orchestrator status` | Show current status |
| `issue-orchestrator attach <issue>` | Attach to a running session |
| `issue-orchestrator pause` | Finish current, don't start new |
| `issue-orchestrator resume` | Resume after pause |
| `issue-orchestrator next <issue>` | Prioritize a specific issue |

## How it works

```
issue-orchestrator
    │
    ├── Fetches issues from GitHub (gh issue list)
    ├── Analyzes dependencies between issues
    ├── Creates git worktree per issue
    ├── Launches Claude in tmux session per issue
    ├── Monitors for:
    │   ├── Session exit → check GitHub for PR/labels
    │   ├── Timeout → kill and mark failed
    │   └── Blocked/needs-human labels → move on
    └── Dashboard UI shows status
```

## Labels

| Label | Meaning |
|-------|---------|
| `agent:*` | Which agent type should work on this |
| `in-progress` | Currently being worked on |
| `blocked` | Agent couldn't complete, needs unblocking |
| `needs-human` | Agent has a question for a human |

## Testing

The orchestrator has been validated with test issues to ensure the workflow completes successfully.

## License

MIT
