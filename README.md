# issue-orchestrator

Orchestrate AI agents working on GitHub issues in parallel.

## What it does

- Pulls issues from GitHub based on labels (e.g., `agent:web`, `agent:mobile`)
- Launches Claude Code (or other AI) sessions in isolated git worktrees
- Monitors sessions for completion, blocked state, or need for human input
- Manages concurrency (N parallel sessions)
- Provides a dashboard (web, tmux, or iTerm2) to monitor progress

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
- `tmux` (for tmux mode)
- `git`
- `claude` CLI (Claude Code)

## Quick Start

1. Create a config file in your repo root:

```yaml
# .issue-orchestrator.yaml
agents:
  "agent:web":
    prompt: ".issue-orchestrator/prompts/web.md"
    worktree_base: "../"

concurrency:
  max_sessions: 3
```

2. Create a prompt file for your agents (see [Agent Prompts](#agent-prompts))

3. Run the orchestrator:

```bash
issue-orchestrator start
```

## Commands

| Command | Description |
|---------|-------------|
| `issue-orchestrator start` | Start the orchestrator |
| `issue-orchestrator start --ui-mode web` | Start with web dashboard (default :8080) |
| `issue-orchestrator start --ui-mode web --port 3000` | Web dashboard on custom port |
| `issue-orchestrator start --ui-mode iterm2` | Start with iTerm2 tabs (macOS) |
| `issue-orchestrator start --test-mode` | Run with mock test data |
| `issue-orchestrator start --milestone "v1.0"` | Filter to issues in milestone |
| `issue-orchestrator start --dry-run` | Show what would run without launching |
| `issue-orchestrator start --debug` | Enable verbose logging to ~/.issue-orchestrator.log |
| `issue-orchestrator start --no-dashboard` | Run headless (for CI/scripting) |
| `issue-orchestrator status` | Show current status |
| `issue-orchestrator attach <issue>` | Attach to a running session |
| `issue-orchestrator pause` | Finish current, don't start new |
| `issue-orchestrator resume` | Resume after pause |
| `issue-orchestrator next <issue>` | Prioritize a specific issue |

## Configuration

Full configuration options:

```yaml
# .issue-orchestrator.yaml

# Agent configurations - map issue labels to prompts
agents:
  "agent:backend":
    prompt: ".issue-orchestrator/prompts/backend.md"
    worktree_base: "../"           # Where to create worktrees
    model: "sonnet"                # Claude model
    timeout_minutes: 45            # Session timeout
    # Optional: custom command (default uses claude CLI)
    # command: "claude --model {model} '{initial_prompt}'"
    # Optional: different repo root for this agent
    # repo_root: "/path/to/repo"

# Concurrency settings
concurrency:
  max_sessions: 3
  session_timeout_minutes: 45

# Label names (customize if your repo uses different labels)
labels:
  in_progress: "in-progress"
  blocked: "blocked"
  needs_human: "needs-human"
  prefix: "bot"                    # Optional: prefix all labels (e.g., "bot:in-progress")

# UI mode: "tmux" (default), "iterm2", or "web"
ui_mode: "tmux"

# Tab cleanup behavior
close_completed_tabs: true         # Auto-close tabs for successful completions
close_failed_tabs: false           # Keep failed tabs open for investigation

# Enforcement options
enforce_hooks: true                # Install pre-push hooks to enforce agent-done
pre_push_hook: "./my-hook.sh"      # Optional: custom pre-push hook path

# Optional: only process issues with this label
# filter_label: "test-data"

# Optional: only process issues in this milestone
# filter_milestone: "v1.0"

# Optional: specify repo explicitly (otherwise auto-detected)
# repo: "owner/repo-name"

# Optional: custom comment headings
comment_headings:
  implementation: "## Implementation"
  problems: "## Problems Encountered"
  pr_link: "## Pull Request"
  blocked: "## Blocked"
  needs_human: "## Needs Human Input"
```

## Example Flow

Here's a complete walkthrough of what happens when an issue is processed:

### 1. Issue Created with Agent Label

```
GitHub Issue #42: "Add user authentication"
Labels: [agent:backend, priority:high]
```

The `agent:backend` label tells the orchestrator which agent config to use.

### 2. Orchestrator Picks Up the Issue

```
$ issue-orchestrator start

[Orchestrator] Fetching issues with agent labels...
[Orchestrator] Found issue #42 with label 'agent:backend'
[Orchestrator] Matched to agent config 'agent:backend'
```

Config mapping:
```yaml
agents:
  "agent:backend":                        # ← Matches label on issue
    prompt: "prompts/backend.md"          # ← Instructions for this agent type
    worktree_base: "../"
```

### 3. Worktree Created

The orchestrator creates an isolated git worktree for the agent:

```
Repo:      /home/dev/myapp
Worktree:  /home/dev/myapp-42              # {worktree_base}/{repo}-{issue}
Branch:    42-add-user-authentication      # {issue}-{slugified-title}
```

```bash
# What happens under the hood:
git worktree add ../myapp-42 -b 42-add-user-authentication
```

### 4. Pre-push Hook Installed

If `enforce_hooks: true`, a git hook is copied to the worktree:

```
/home/dev/myapp-42/.git/hooks/pre-push
```

This hook will block any `git push` that doesn't have proper `Agent-Status:` trailers.

### 5. Agent Session Launched

The orchestrator launches Claude in a tmux/iTerm2/web session:

```bash
cd /home/dev/myapp-42
claude --model sonnet "You are working on issue #42: Add user authentication.
Read prompts/backend.md for full instructions."
```

The issue is labeled `in-progress` on GitHub.

### 6. Agent Does Work

The AI agent:
- Reads the prompt file
- Implements the feature
- Writes tests
- Makes commits

```bash
# Agent commits work
git add .
git commit -m "feat: add user authentication middleware"
```

### 7. Agent Completes with `agent-done`

The agent **cannot** just `git push`. It must use `agent-done`:

```bash
agent-done completed \
  --implementation "Added JWT auth middleware and login endpoint" \
  --problems "None"
```

### 8. What `agent-done` Does

**Step 8a: Add trailers to commit**
```bash
# Amends last commit to add structured trailers:
git commit --amend -m "feat: add user authentication middleware

Agent-Status: completed
Agent-Implementation: Added JWT auth middleware and login endpoint
Agent-Problems: None"
```

**Step 8b: Push (hook validates)**
```bash
git push -u origin 42-add-user-authentication

# Pre-push hook checks:
# ✓ Agent-Status trailer exists
# ✓ Status is valid (completed/blocked/needs_human)
# ✓ Required fields present for status type
# → Push allowed
```

**Step 8c: Create PR**
```bash
gh pr create --title "Fix #42" --body "Closes #42..."
# Returns: https://github.com/owner/repo/pull/99
```

**Step 8d: Post comment on issue**
```markdown
## Implementation
Added JWT auth middleware and login endpoint

## Problems Encountered
None

## Pull Request
https://github.com/owner/repo/pull/99
```

### 9. Orchestrator Detects Completion

```
[Orchestrator] Session for #42 exited
[Orchestrator] Checking GitHub...
[Orchestrator] ✓ PR #99 found
[Orchestrator] Status: COMPLETED
[Orchestrator] Removing 'in-progress' label
[Orchestrator] Cleaning up worktree...
```

### 10. What If Something Goes Wrong?

**If agent is blocked:**
```bash
agent-done blocked \
  --reason "Need AWS credentials for S3 integration" \
  --attempted "Checked env vars, secrets manager, asked in comments"
```
→ Adds `blocked` label, posts comment explaining the block

**If agent needs human input:**
```bash
agent-done needs_human \
  --question "Should we use JWT or session-based auth?" \
  --options "JWT tokens" "Session cookies" \
  --default "JWT tokens"
```
→ Adds `needs-human` label, posts question for human to answer

**If agent tries to push directly (without agent-done):**
```
$ git push
ERROR: Missing Agent-Status trailer in latest commit

You must use 'agent-done' to complete your work:
  agent-done completed --implementation '...' --problems '...'
  agent-done blocked --reason '...' --attempted '...'
  agent-done needs_human --question '...'

Direct 'git push' is not allowed without structured status.
```

### Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         GITHUB                                  │
│  Issue #42 [agent:backend]                                      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      ORCHESTRATOR                               │
│  • Polls for issues with agent:* labels                         │
│  • Matches label → agent config                                 │
│  • Creates worktree + installs hooks                            │
│  • Launches Claude session                                      │
│  • Monitors for completion                                      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    GIT WORKTREE                                 │
│  ../myapp-42/                                                   │
│  ├── (isolated copy of repo)                                    │
│  ├── .git/hooks/pre-push  ← Enforces agent-done                 │
│  └── Branch: 42-add-user-authentication                         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    CLAUDE SESSION                               │
│  • Reads issue + prompt                                         │
│  • Implements fix                                               │
│  • Commits changes                                              │
│  • Runs: agent-done completed --implementation "..." ...        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     agent-done                                  │
│  1. Add trailers to commit (Agent-Status: completed, etc)       │
│  2. git push (hook validates trailers)                          │
│  3. gh pr create                                                │
│  4. gh issue comment (structured comment)                       │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                         GITHUB                                  │
│  Issue #42 ← Comment with implementation details                │
│  PR #99 ← Links to issue, ready for review                      │
└─────────────────────────────────────────────────────────────────┘
```

## Label Conventions

### Agent Labels
Issues are routed to agents based on labels like `agent:web`, `agent:backend`, etc.
These must match the keys in your `agents:` config.

### Status Labels
| Label | Meaning |
|-------|---------|
| `in-progress` | Currently being worked on (set by orchestrator) |
| `blocked` | Agent couldn't complete (set by agent) |
| `needs-human` | Agent has a question (set by agent) |

### Label Prefixing
If your repo has many labels, use the `prefix` option to namespace orchestrator labels:

```yaml
labels:
  prefix: "bot"
```

This turns `in-progress` → `bot:in-progress`, etc.

## Agent Prompts

Prompts are markdown files that instruct the AI what to do. They receive template variables:

| Variable | Description |
|----------|-------------|
| `{issue_number}` | GitHub issue number |
| `{issue_title}` | Issue title |
| `{worktree}` | Path to the worktree |

Example prompt:
```markdown
# Fix Issue #{issue_number}

You are working on: {issue_title}

## Instructions
1. Read the issue carefully
2. Implement the fix
3. Write tests
4. Complete using `agent-done`

## Completion (MANDATORY)

Use the `agent-done` command to complete:

\`\`\`bash
agent-done completed --implementation "What you did" --problems "None"
\`\`\`

If blocked:
\`\`\`bash
agent-done blocked --reason "Why" --attempted "What you tried"
\`\`\`

If you need human input:
\`\`\`bash
agent-done needs_human --question "Your question"
\`\`\`
```

## The `agent-done` Command

Agents **must** use `agent-done` to complete their work. This command:
1. Adds structured trailers to the last commit
2. Pushes the code
3. Creates a PR (for completions)
4. Posts a structured comment on the issue
5. Adds appropriate labels

### Usage

```bash
# Successfully completed
agent-done completed --implementation "Added auth middleware" --problems "None"

# Blocked
agent-done blocked --reason "Need API credentials" --attempted "Checked env vars"

# Need human input
agent-done needs_human --question "Should we use OAuth or API keys?"
```

### Enforcement

When `enforce_hooks: true`, the orchestrator installs a pre-push hook that:
- Blocks `git push` unless commits have proper `Agent-Status:` trailers
- Forces agents to use `agent-done` instead of pushing directly

## Web Dashboard

Start with `--ui-mode web` to get a browser dashboard on port 8080:

```bash
issue-orchestrator start --ui-mode web
```

Features:
- Real-time status of all sessions
- Attach to running sessions
- View issue details and comments
- Pause/resume orchestration

## Git Worktrees

Each issue gets an isolated worktree:
- Location: `{worktree_base}/{repo_name}-{issue_number}`
- Branch: `{issue_number}-{slugified-title}`
- Example: `../myrepo-123` with branch `123-add-user-auth`

Worktrees are cleaned up after session completion (configurable).

## Testing

Use the `--test-mode` flag to run with mock data:

```bash
issue-orchestrator start --test-mode
```

Or filter to specific issues:

```yaml
filter_label: "test-data"
```

## CTO Review Agent

The structured comments created by `agent-done` are designed to be parseable by other agents.
A "CTO Review" agent can audit work by reading these structured comments:

### How It Works

1. **Worker agent completes issue #42**
   - Posts structured comment with `## Implementation`, `## Problems Encountered`, `## Pull Request`

2. **CTO agent is triggered** (manually or via `agent:cto-review` label)
   - Reads issue comments: `gh issue view 42 --comments`
   - Parses structured sections from worker's comment
   - Reviews the PR diff: `gh pr diff <pr_number>`

3. **CTO agent posts review**
   ```markdown
   ## CTO Review

   ### Summary
   - Brief assessment of the work

   ### Problems Analysis
   - Problems reported by agent: <from "Problems Encountered">
   - Pre-existing issues found: <test failures, tech debt discovered>
   - New concerns: <anything noticed>

   ### Recommendations
   - Suggestions for improvement
   - Follow-up issues to create

   ### Status
   - [ ] Approved for merge
   - [ ] Needs changes
   - [ ] Escalate to human
   ```

### Why This Matters

- **Surfaces technical debt**: The `## Problems Encountered` section captures issues the worker discovered
- **Automated review**: CTO agent can batch-review completed work
- **Audit trail**: All work is documented with structured, parseable comments
- **Escalation path**: CTO can flag work that needs human attention

### Example Config

```yaml
agents:
  "agent:backend":
    prompt: "prompts/backend.md"

  "agent:cto-review":
    prompt: "prompts/cto-review.md"
    # CTO agent gets assigned to review issues after workers complete them
```

See `examples/prompts/cto-review.md` for the full CTO agent prompt.

## Protocol

See [AGENT_PROTOCOL.md](./AGENT_PROTOCOL.md) for the full contract between
orchestrator and agents.

## License

MIT
