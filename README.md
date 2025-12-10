# issue-orchestrator

Orchestrate AI agents working on GitHub issues in parallel.

## What it does

- Pulls issues from GitHub based on labels (e.g., `agent:web`, `agent:mobile`)
- Launches Claude Code (or other AI) sessions in isolated git worktrees
- Monitors sessions for completion, blocked state, or need for human input
- Manages concurrency (N parallel sessions)
- Provides a dashboard (web, tmux, or iTerm2) to monitor progress

## Key Concepts

Before diving in, understand these core concepts:

### Agent Labels

Issues are assigned to agents via GitHub labels. An issue with label `agent:backend` gets picked up by the agent configured under that key:

```yaml
agents:
  "agent:backend":     # ← Matches the GitHub label
    prompt: "prompts/backend.md"
```

You define which agents exist by adding entries to your config. Common setups:
- Single agent: `agent:claude` for all issues
- By domain: `agent:backend`, `agent:frontend`, `agent:mobile`
- By task type: `agent:bugfix`, `agent:feature`

### Worktrees

Each issue gets an isolated [git worktree](https://git-scm.com/docs/git-worktree)—a separate checkout with its own branch. This lets multiple agents work in parallel without conflicts:

```
your-repo/          ← main repo
your-repo-42/       ← worktree for issue #42, branch: 42-add-auth
your-repo-57/       ← worktree for issue #57, branch: 57-fix-bug
```

Worktrees are cleaned up after successful completion. Configure cleanup with `close_completed_tabs` and `close_failed_tabs` in your config.

### UI Modes

The orchestrator provides three ways to monitor sessions:

| Mode | Description |
|------|-------------|
| **web** (default) | Browser dashboard at http://localhost:8080 |
| **tmux** | Terminal multiplexer with panes per session |
| **iterm2** | macOS iTerm2 tabs (requires iTerm2) |

### The `agent-done` Workflow

Agents cannot just `git push`. They must complete work using the `agent-done` command, which:
1. Adds structured metadata to their commit
2. Pushes and creates a PR
3. Posts a structured comment on the issue
4. Updates labels appropriately

This ensures all agent work is tracked and auditable.

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
- `git`
- `claude` CLI (Claude Code)
- `tmux` (only for tmux mode)

## Quick Start

1. Create a config file in your repo root:

```yaml
# .issue-orchestrator.yaml
agents:
  "agent:backend":                    # Label on GitHub issues
    prompt: ".issue-orchestrator/prompts/backend.md"

concurrency:
  max_concurrent_sessions: 3
```

2. Create a prompt file for your agents (see [Agent Prompts](#agent-prompts))

3. Label some GitHub issues with `agent:backend`

4. Run the orchestrator:

```bash
issue-orchestrator start
```

This opens a web dashboard at http://localhost:8080 showing active sessions and the issue queue.

## Commands

| Command | Description |
|---------|-------------|
| `issue-orchestrator start` | Start with web dashboard (default) |
| `issue-orchestrator start --port 3000` | Web dashboard on custom port |
| `issue-orchestrator start --ui-mode tmux` | Use tmux instead of web |
| `issue-orchestrator start --ui-mode iterm2` | Use iTerm2 tabs (macOS) |
| `issue-orchestrator start --test-mode` | Run with mock test data |
| `issue-orchestrator start --milestone "v1.0"` | Filter to issues in milestone |
| `issue-orchestrator start --max-issues 5` | Limit to N issues this session |
| `issue-orchestrator start --dry-run` | Show what would run without launching |
| `issue-orchestrator start --debug` | Enable verbose logging to ~/.issue-orchestrator.log |
| `issue-orchestrator start --no-dashboard` | Run headless (for CI/scripting) |
| `issue-orchestrator --config /path/config.yaml start` | Use custom config file |
| `issue-orchestrator -c ./other-config.yaml status` | Short form of --config |
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
  max_concurrent_sessions: 3
  session_timeout_minutes: 45

# Label names (customize if your repo uses different labels)
labels:
  in_progress: "in-progress"
  blocked: "blocked"
  needs_human: "needs-human"
  prefix: "bot"                    # Optional: prefix all labels (e.g., "bot:in-progress")

# UI mode: "web" (default), "tmux", or "iterm2"
ui_mode: "web"
web_port: 8080                     # Port for web dashboard

# Tab/worktree cleanup behavior
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

# Optional: max issues to fetch per API call (default 100, gh default is 30)
# issue_fetch_limit: 100

# Optional: custom comment headings
comment_headings:
  implementation: "## Implementation"
  problems: "## Problems Encountered"
  pr_link: "## Pull Request"
  blocked: "## Blocked"
  needs_human: "## Needs Human Input"
```

## Setup Guide

This section explains key configuration concepts and how they work together.

### How Agent Labels Connect to Config

The orchestrator uses GitHub labels to route issues to agents. The connection is direct:

```
GitHub Issue Label  →  Config Key  →  Agent Settings
    "agent:web"     →  agents["agent:web"]  →  prompt, model, timeout
```

**Step 1: Define agents in your config**
```yaml
agents:
  "agent:backend":
    prompt: "prompts/backend.md"
  "agent:frontend":
    prompt: "prompts/frontend.md"
```

**Step 2: Create matching labels on GitHub**
```bash
# Use the init command to create labels automatically
issue-orchestrator init
```

**Step 3: Label issues to assign them to agents**
- Add `agent:backend` label → picked up by backend agent config
- Add `agent:frontend` label → picked up by frontend agent config

Issues without an `agent:*` label are ignored by the orchestrator.

### Priority Labels

Issues are processed in priority order. Add priority labels to control the queue:

| Label | Priority | Description |
|-------|----------|-------------|
| `priority:high` | 1 | Processed first |
| `priority:medium` | 2 | Default priority |
| `priority:low` | 3 | Processed last |
| (no priority label) | 2 | Treated as medium |

Within the same priority level, issues are sorted by milestone (if filtered), then by issue number.

### Milestones and Sorting

Filter by milestone to focus on specific releases:

```bash
# Process only issues in the v2.0 milestone
issue-orchestrator start --milestone "v2.0"
```

Or set a default in your config:
```yaml
filter_milestone: "v2.0"
```

**Milestone Sorting Strategies**

When processing issues in a milestone, you can control the sort order:

```yaml
# Sort milestones by due date (default)
milestone_sort: "due_date"

# Sort by milestone number (M1, M2, M3)
milestone_sort: "number"

# Sort by name alphabetically
milestone_sort: "name"

# Sort by pattern extraction (e.g., "Sprint 3" → 3)
milestone_sort: "pattern"
milestone_sort_config:
  pattern: "Sprint (\\d+)"
```

### Concurrency Settings

Two settings control how many issues are processed:

**`max_concurrent_sessions`** - How many agents run in parallel
```yaml
concurrency:
  max_concurrent_sessions: 3   # Run up to 3 agents at once
```

This is your parallelism limit. Set based on your machine's resources and API rate limits.

**`max_issues_to_start`** - Total issues to start this session
```yaml
max_issues_to_start: 10   # Start at most 10 issues, then stop
```

Use this to:
- Limit a test run: `issue-orchestrator start --max-issues 2`
- Process a batch without running forever
- Control costs/API usage

Set to `0` (default) for unlimited.

**Example: Controlled batch processing**
```bash
# Process up to 5 issues, 2 at a time
issue-orchestrator start --max-issues 5

# In config:
concurrency:
  max_concurrent_sessions: 2
max_issues_to_start: 5
```

### Config File Location

By default, the orchestrator searches for config in:
1. `.issue-orchestrator.yaml` in current directory
2. `.issue-orchestrator/config.yaml` in current directory
3. Same paths in parent directories (walks up to repo root)

To use a different config file:
```bash
issue-orchestrator --config /path/to/my-config.yaml start
issue-orchestrator -c ./configs/production.yaml start
```

This is useful for:
- Multiple config profiles (dev, staging, production)
- Testing different configurations
- CI/CD environments with custom paths

## Web Dashboard

The web dashboard (default mode) provides:

- Real-time status of all active sessions
- Issue queue showing what's next
- Attach to running sessions
- View issue details and comments
- Pause/resume orchestration

Start on a custom port:
```bash
issue-orchestrator start --port 3000
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
