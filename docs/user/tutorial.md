# Issue Orchestrator Tutorial

A hands-on guide to setting up and using issue-orchestrator to run AI agents on GitHub issues in parallel.

## Table of Contents

1. [What is Issue Orchestrator?](#what-is-issue-orchestrator)
2. [Prerequisites](#prerequisites)
3. [Installation](#installation)
4. [Quick Setup Walkthrough](#quick-setup-walkthrough)
5. [Understanding the System](#understanding-the-system)
6. [Creating Agent Prompts](#creating-agent-prompts)
7. [Setting Up Safety Hooks](#setting-up-safety-hooks)
8. [Creating Issues for Agents](#creating-issues-for-agents)
9. [Running the Orchestrator](#running-the-orchestrator)
10. [How an Issue Flows Through the System](#how-an-issue-flows-through-the-system)
11. [Monitoring and Managing Sessions](#monitoring-and-managing-sessions)
12. [Troubleshooting](#troubleshooting)
13. [Next Steps](#next-steps)

---

## What is Issue Orchestrator?

Issue Orchestrator automates the process of having AI agents (like Claude Code) work on GitHub issues in parallel. Instead of manually running Claude on one issue at a time, you can:

- Label issues with agent tags (e.g., `agent:backend`, `agent:frontend`)
- Run the orchestrator to automatically pick up and process those issues
- Monitor progress through a web dashboard, tmux, or iTerm2 tabs
- Have agents create PRs and report their work in structured comments

Think of it as a supervisor that:
1. Pulls issues from your GitHub repo based on labels
2. Creates isolated git worktrees for each issue
3. Launches AI agent sessions to work on them
4. Monitors for completion, blocks, or questions
5. Cleans up and moves to the next issue

---

## Prerequisites

Before you begin, make sure you have:

### Required

| Tool | Purpose | Installation |
|------|---------|--------------|
| **Python 3.11+** | Runs the orchestrator | `brew install python` or from python.org |
| **git** | Version control | Usually pre-installed |
| **GitHub CLI (`gh`)** | Interacts with GitHub | `brew install gh` then `gh auth login` |
| **Claude CLI** | The AI agent | See [Claude Code docs](https://claude.ai/claude-code) |

### Optional (depending on UI mode)

| Tool | Purpose | Installation |
|------|---------|--------------|
| **tmux** | Terminal multiplexer UI mode | `brew install tmux` |
| **iTerm2** | macOS terminal with tabs | Download from iterm2.com |
| **jq** | JSON processing (for hooks) | `brew install jq` |

### Verify Your Setup

```bash
# Check Python version
python3 --version  # Should be 3.11 or higher

# Check git
git --version

# Check GitHub CLI is authenticated
gh auth status

# Check Claude CLI
claude --version
```

---

## Installation

### From PyPI (Recommended)

```bash
pip install issue-orchestrator
```

### From Source (For Development)

```bash
git clone https://github.com/BruceBGordon/issue-orchestrator
cd issue-orchestrator
pip install -e ".[dev]"
```

### Verify Installation

```bash
issue-orchestrator --version
issue-orchestrator --help
```

---

## Quick Setup Walkthrough

Let's set up issue-orchestrator for a sample project step by step.

### Step 1: Navigate to Your Project

```bash
cd /path/to/your/project
```

### Step 2: Create Configuration File

Create `.issue-orchestrator.yaml` in your project root:

```yaml
# .issue-orchestrator.yaml

# Your GitHub repo (auto-detected if not specified)
repo: your-username/your-repo

# Agent configurations - the keys match GitHub labels
agents:
  "agent:claude":                           # This label on an issue...
    prompt: ".issue-orchestrator/prompts/worker.md"  # ...uses this prompt
    model: "sonnet"                         # Claude model to use
    timeout_minutes: 45                     # Max time before timeout

# How many agents to run at once
concurrency:
  max_concurrent_sessions: 2
  session_timeout_minutes: 45

# UI mode: "web" (browser), "tmux", or "iterm2"
ui_mode: "web"
web_port: 8080
```

### Step 3: Create the Prompt Directory and File

```bash
mkdir -p .issue-orchestrator/prompts
```

Create `.issue-orchestrator/prompts/worker.md`:

```markdown
# Issue Worker

You are working on GitHub issue #{issue_number}: {issue_title}

## Your Task

1. Read the issue carefully
2. Explore the codebase to understand the context
3. Implement the solution
4. Write tests if appropriate
5. Commit your changes with clear messages

## Completion (REQUIRED)

When done, use the `agent-done` command:

### If you completed the work:
```bash
agent-done completed \
  --implementation "What you implemented" \
  --problems "Any issues encountered, or 'None'"
```

### If you're blocked:
```bash
agent-done blocked \
  --reason "Why you cannot proceed" \
  --attempted "What you tried"
```

### If you need human input:
```bash
agent-done needs_human \
  --question "Your specific question"
```

Sessions that exit without calling `agent-done` are marked as failed.
```

### Step 4: Initialize GitHub Labels

The orchestrator uses specific labels. Create them automatically:

```bash
issue-orchestrator init
```

This creates labels like:
- `agent:claude` (or whatever agents you defined)
- `in-progress`
- `blocked`
- `needs-human`
- `priority:high`, `priority:medium`, `priority:low`

### Step 5: Set Up Safety Hooks

Install hooks that prevent agents from bypassing safety guardrails:

```bash
issue-orchestrator setup-hooks
```

This creates:
- `.claude/hooks/block-no-verify.sh` - Blocks `git push --no-verify`
- `.claude/settings.json` - Configures Claude to use the hook

### Step 6: Verify Your Setup

```bash
issue-orchestrator verify
```

This checks:
- Configuration is valid
- GitHub CLI is authenticated
- Git repository is valid
- Hooks are installed and working

If you see `--live` option, you can run `issue-orchestrator verify --live` to spawn Claude and actually test that blocking works.

---

## Understanding the System

### Core Concepts

#### Agent Labels

Issues are routed to agents based on GitHub labels:

```
GitHub Issue Label    →    Config Key         →    Agent Settings
  "agent:backend"     →    agents["agent:backend"]    →    prompt, model, timeout
```

You define which agents exist by adding entries to your config:

```yaml
agents:
  "agent:backend":
    prompt: "prompts/backend.md"
  "agent:frontend":
    prompt: "prompts/frontend.md"
  "agent:devops":
    prompt: "prompts/devops.md"
```

#### Worktrees

Each issue gets its own [git worktree](https://git-scm.com/docs/git-worktree) - an isolated checkout:

```
your-repo/           ← main repo
your-repo-42/        ← worktree for issue #42
your-repo-57/        ← worktree for issue #57
```

This lets multiple agents work in parallel without conflicts.

#### Priority

Issues are processed in priority order:

| Label | Priority |
|-------|----------|
| `priority:high` | First |
| `priority:medium` | Second (default) |
| `priority:low` | Last |

#### Status Labels

| Label | Meaning | Who Sets It |
|-------|---------|-------------|
| `in-progress` | Being worked on | Orchestrator (on launch) |
| `blocked` | Agent hit a blocker | Agent (via `agent-done blocked`) |
| `needs-human` | Agent has a question | Agent (via `agent-done needs_human`) |

---

## Creating Agent Prompts

Prompts are markdown files that instruct the AI what to do. They're the "personality" and instructions for each agent type.

### Template Variables

Prompts receive these variables:

| Variable | Description | Example |
|----------|-------------|---------|
| `{issue_number}` | GitHub issue number | `42` |
| `{issue_title}` | Issue title | `Add user authentication` |
| `{worktree}` | Path to the worktree | `/home/dev/myapp-42` |
| `{prompt}` | Path to this prompt file | `prompts/backend.md` |

### Example: Backend Agent

`.issue-orchestrator/prompts/backend.md`:

```markdown
# Backend Agent

You are a backend engineer working on issue #{issue_number}: {issue_title}

## Expertise

- Python, FastAPI, SQLAlchemy
- PostgreSQL, Redis
- REST API design
- Testing with pytest

## Guidelines

1. Follow existing code patterns in the codebase
2. Write tests for new functionality
3. Keep functions small and focused
4. Use type hints
5. Update docstrings

## Workflow

1. Read the issue and understand requirements
2. Explore relevant files (`src/api/`, `src/models/`, `tests/`)
3. Implement the solution
4. Write/update tests
5. Run `pytest` to verify
6. Commit with conventional commit messages

## Completion

Use `agent-done` when finished:

```bash
# Success
agent-done completed \
  --implementation "Added /users endpoint with JWT auth" \
  --problems "None"

# Blocked
agent-done blocked \
  --reason "Need database credentials" \
  --attempted "Checked .env, asked in issue comments"

# Need help
agent-done needs_human --question "Should we use OAuth or API keys?"
```

**Important**: Report ALL problems honestly. The triage review agent will catch unreported issues.
```

### Example: Frontend Agent

`.issue-orchestrator/prompts/frontend.md`:

```markdown
# Frontend Agent

You are a frontend engineer working on issue #{issue_number}: {issue_title}

## Expertise

- React, TypeScript
- Tailwind CSS
- React Query for data fetching
- Testing with Vitest and React Testing Library

## Guidelines

1. Use functional components with hooks
2. Follow the existing component structure
3. Ensure accessibility (ARIA labels, keyboard nav)
4. Write component tests

## Completion

Use `agent-done` when finished - see `agent-done --help` for options.
```

### Multi-Agent Setup

For larger projects, you might have specialized agents:

```yaml
agents:
  "agent:backend":
    prompt: "prompts/backend.md"
    model: "sonnet"
    timeout_minutes: 45

  "agent:frontend":
    prompt: "prompts/frontend.md"
    model: "sonnet"
    timeout_minutes: 30

  "agent:devops":
    prompt: "prompts/devops.md"
    model: "haiku"        # Smaller model for simpler tasks
    timeout_minutes: 20

  "agent:docs":
    prompt: "prompts/docs.md"
    model: "haiku"
    skip_review: true     # Don't require code review for docs
```

---

## Setting Up Safety Hooks

Hooks prevent agents from bypassing safety guardrails. This is **critical** - without hooks, agents can:
- Skip pre-commit/pre-push tests with `--no-verify`
- Push broken code directly
- Create PRs without going through `agent-done`

### Why This Matters

AI agents are clever and will find shortcuts. If you tell them "don't use --no-verify" in a prompt, they might:
- Forget
- Decide it's "just this once"
- Find alternative ways to skip hooks

Technical enforcement is the only reliable solution.

### Installing Hooks

```bash
issue-orchestrator setup-hooks
```

This creates:

**`.claude/hooks/block-no-verify.sh`**
```bash
#!/bin/bash
# Intercepts commands before Claude executes them
# Exit 2 = BLOCK, Exit 0 = ALLOW

input=$(cat)
command=$(echo "$input" | jq -r '.tool_input.command // ""')

if echo "$command" | grep -qE "git\s+(commit|push).*--no-verify"; then
  echo "BLOCKED: --no-verify is forbidden." >&2
  exit 2
fi

exit 0
```

**`.claude/settings.json`**
```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": ".claude/hooks/block-no-verify.sh"
          }
        ]
      }
    ]
  }
}
```

### Verifying Hooks Work

```bash
# Quick verification (tests hook script logic)
issue-orchestrator verify

# Live verification (actually spawns Claude to test blocking)
issue-orchestrator verify --live
```

The live verification:
1. Creates a temp git repo
2. Spawns Claude with a prompt to run `git push --no-verify`
3. Verifies Claude reports being blocked

### What Happens If Hooks Fail

If verification fails, the orchestrator won't start by default:

```
STARTUP BLOCKED: Hook verification failed

Without verified hooks, agents can bypass --no-verify
and push code without running pre-push tests/checks.

Options:
  1. Run 'issue-orchestrator setup-hooks' to install hooks
  2. Run 'issue-orchestrator verify' to diagnose issues
```

---

## Creating Issues for Agents

Now that your system is configured, create GitHub issues for agents to work on.

### Issue Requirements

For an issue to be picked up by the orchestrator:

1. **Must have an agent label** matching a key in your config (e.g., `agent:backend`)
2. **Should have a priority label** (optional, defaults to medium)
3. **Should have a clear description** so the agent understands the task

### Example Issue

**Title**: Add user profile endpoint

**Labels**: `agent:backend`, `priority:high`

**Body**:
```markdown
## Summary

Add a GET /users/{id}/profile endpoint that returns user profile information.

## Requirements

- Return user's name, email, bio, and avatar URL
- Return 404 if user doesn't exist
- Include rate limiting (10 requests/minute)

## Acceptance Criteria

- [ ] Endpoint returns correct data for existing users
- [ ] Returns 404 with appropriate message for missing users
- [ ] Rate limiting works correctly
- [ ] Tests cover success and error cases
```

### Creating Issues via CLI

```bash
# Create an issue
gh issue create \
  --title "Add user profile endpoint" \
  --body "..." \
  --label "agent:backend" \
  --label "priority:high"

# Or create and let the orchestrator pick it up
gh issue create \
  --title "Fix login bug" \
  --label "agent:frontend"
```

### Tips for Good Agent Issues

1. **Be specific** - Agents work best with clear, bounded tasks
2. **Provide context** - Link to relevant files or docs
3. **Include acceptance criteria** - How will we know it's done?
4. **One task per issue** - Don't bundle multiple unrelated changes
5. **Use checklists** - Agents can check off items as they complete them

---

## Running the Orchestrator

### Basic Usage

```bash
# Start with web dashboard (default)
issue-orchestrator start
```

This opens a browser at http://localhost:8080 showing:
- Active sessions (agents currently working)
- Issue queue (what's waiting to be processed)
- Status and controls

### Command Line Options

```bash
# Use tmux instead of web dashboard
issue-orchestrator start --ui-mode tmux

# Use iTerm2 tabs (macOS only)
issue-orchestrator start --ui-mode iterm2

# Custom port for web dashboard
issue-orchestrator start --port 3000

# Limit to specific milestone
issue-orchestrator start --milestone "v2.0"

# Only process N issues then stop
issue-orchestrator start --max-issues 5

# Dry run - show what would happen without doing it
issue-orchestrator start --dry-run

# Enable debug logging (writes to ~/.issue-orchestrator.log)
issue-orchestrator start --debug

# Run without dashboard (for CI/scripts)
issue-orchestrator start --no-dashboard
```

### Other Commands

```bash
# Show current status
issue-orchestrator status

# Attach to a running session
issue-orchestrator attach 42

# Pause (finish current work, don't start new)
issue-orchestrator pause

# Resume after pause
issue-orchestrator resume

# Prioritize a specific issue
issue-orchestrator next 42

# Initialize labels on GitHub
issue-orchestrator init

# Verify setup
issue-orchestrator verify

# Install hooks
issue-orchestrator setup-hooks
```

---

## How an Issue Flows Through the System

Let's trace what happens when issue #42 is processed.

### 1. Issue Created and Labeled

```
GitHub Issue #42: "Add user profile endpoint"
Labels: [agent:backend, priority:high]
```

### 2. Orchestrator Polls and Finds Issue

```
$ issue-orchestrator start

[Orchestrator] Fetching issues with agent labels...
[Orchestrator] Found 3 issues
[Orchestrator] Queue sorted by priority:
  1. #42 (priority:high) → agent:backend
  2. #57 (priority:medium) → agent:frontend
  3. #63 (priority:low) → agent:backend
[Orchestrator] Starting #42...
```

### 3. Worktree Created

```
[launch] Creating worktree for issue #42...
         Repo: /home/dev/myapp
         Worktree: /home/dev/myapp-42
         Branch: 42-add-user-profile-endpoint

$ git worktree add ../myapp-42 -b 42-add-user-profile-endpoint
```

### 4. Safety Hooks Installed

The orchestrator copies pre-push hooks to the worktree:

```
/home/dev/myapp-42/.git/hooks/pre-push
```

This ensures any push from this worktree runs project tests.

### 5. Issue Labeled `in-progress`

```
[Orchestrator] Adding 'in-progress' label to #42
```

### 6. Claude Session Launched

```
[launch] Starting Claude session...

$ cd /home/dev/myapp-42
$ claude --model sonnet "You are working on issue #42: Add user profile endpoint.
  Read prompts/backend.md for your instructions."
```

A new terminal tab/tmux window/web session is created showing Claude working.

### 7. Agent Works on the Issue

Claude:
1. Reads the prompt file
2. Reads the issue details via `gh issue view 42`
3. Explores relevant code
4. Implements the solution
5. Writes tests
6. Makes commits

```bash
# Agent's commits
git add src/api/profile.py tests/test_profile.py
git commit -m "feat: add user profile endpoint"

git add src/api/rate_limit.py
git commit -m "feat: add rate limiting to profile endpoint"
```

### 8. Agent Completes with `agent-done`

```bash
agent-done completed \
  --implementation "Added GET /users/{id}/profile endpoint with rate limiting" \
  --problems "None - implementation was straightforward"
```

### 9. What `agent-done` Does

**Step 1: Add trailers to commit**
```bash
git commit --amend -m "feat: add rate limiting to profile endpoint

Agent-Status: completed
Agent-Implementation: Added GET /users/{id}/profile endpoint with rate limiting
Agent-Problems: None - implementation was straightforward"
```

**Step 2: Push (hooks run)**
```bash
git push -u origin 42-add-user-profile-endpoint

# Pre-push hook runs:
# ✓ Project tests pass
# ✓ Agent-Status trailer found
# → Push allowed
```

**Step 3: Create PR**
```bash
gh pr create \
  --title "Fixes #42: Add user profile endpoint" \
  --body "..."

# Returns: https://github.com/owner/repo/pull/99
```

**Step 4: Comment on issue**
```markdown
## Implementation
Added GET /users/{id}/profile endpoint with rate limiting

## Problems Encountered
None - implementation was straightforward

## Pull Request
https://github.com/owner/repo/pull/99
```

### 10. Orchestrator Detects Completion

```
[Monitor] Session for #42 exited
[Monitor] Checking for PR...
[Monitor] ✓ PR #99 found for branch 42-add-user-profile-endpoint
[Monitor] Status: COMPLETED
[Monitor] Removing 'in-progress' label from #42
[Monitor] Cleaning up worktree...

$ git worktree remove ../myapp-42
```

### 11. Next Issue Starts

```
[Orchestrator] Slot available, starting next issue...
[Orchestrator] Starting #57...
```

### Flow Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                        GITHUB                                 │
│  Issue #42 [agent:backend, priority:high]                     │
│  "Add user profile endpoint"                                  │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                     ORCHESTRATOR                              │
│  1. Polls GitHub for issues with agent:* labels               │
│  2. Sorts by priority                                         │
│  3. Creates worktree at ../myapp-42                           │
│  4. Installs pre-push hooks                                   │
│  5. Labels issue "in-progress"                                │
│  6. Launches Claude session                                   │
│  7. Monitors for completion                                   │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                   GIT WORKTREE                                │
│  ../myapp-42/                                                 │
│  ├── (full copy of repo)                                      │
│  ├── .git/hooks/pre-push    ← Enforces tests before push     │
│  └── Branch: 42-add-user-profile-endpoint                     │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                  CLAUDE SESSION                               │
│  • Reads issue and prompt                                     │
│  • Implements solution                                        │
│  • Writes tests                                               │
│  • Commits changes                                            │
│  • Runs: agent-done completed --implementation "..."          │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                    agent-done                                 │
│  1. Adds trailers to commit (Agent-Status: completed)         │
│  2. git push (pre-push hook runs tests)                       │
│  3. gh pr create                                              │
│  4. gh issue comment (structured comment)                     │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                        GITHUB                                 │
│  Issue #42 ← Structured comment with implementation details   │
│  PR #99 ← Links to issue, ready for review                    │
│  Labels: (in-progress removed)                                │
└──────────────────────────────────────────────────────────────┘
```

---

## Monitoring and Managing Sessions

### Web Dashboard

The default web dashboard at http://localhost:8080 shows:

**Active Sessions**
- Issue number and title
- Agent type
- Runtime
- Status (running, completing, etc.)
- Button to attach

**Issue Queue**
- Pending issues sorted by priority
- Labels and milestone
- Estimated wait time

**Controls**
- Pause/Resume orchestration
- Force refresh
- View logs

### Terminal UI (tmux)

```bash
issue-orchestrator start --ui-mode tmux
```

Creates a tmux session with windows for each agent:
- Use `Ctrl-b n` / `Ctrl-b p` to switch between windows
- Use `Ctrl-b d` to detach (orchestrator keeps running)
- Reattach with `tmux attach`

### Attaching to Sessions

To interact with a running agent:

```bash
# From command line
issue-orchestrator attach 42

# This opens the terminal/tab where agent for #42 is running
```

### Status Check

```bash
$ issue-orchestrator status

Issue Orchestrator Status
========================

Active Sessions: 2/3
  #42: agent:backend - 12m running - "Add user profile"
  #57: agent:frontend - 3m running - "Fix login form"

Queue: 5 issues
  1. #63 (high) agent:backend - "Database migration"
  2. #71 (medium) agent:frontend - "Update dashboard"
  ...

Last activity: 30s ago
```

---

## Troubleshooting

### Issue Not Being Picked Up

**Check 1: Does it have an agent label?**
```bash
gh issue view 42 --json labels
```
Must have a label matching a key in your `agents:` config.

**Check 2: Is it already in-progress?**
Issues with `in-progress` label are skipped. Remove it to retry:
```bash
gh issue edit 42 --remove-label "in-progress"
```

**Check 3: Is there a filter blocking it?**
Check your config for `filter_label` or `filter_milestone`.

### Agent Session Fails

**Check worktree exists:**
```bash
ls ../your-repo-42/
```

**Check for errors in the session:**
```bash
issue-orchestrator attach 42
# or check logs
cat ~/.issue-orchestrator.log
```

**Common causes:**
- Missing dependencies in worktree
- Prompt file not found
- Git conflicts
- API rate limits

### Hooks Not Blocking

**Verify hooks are installed:**
```bash
issue-orchestrator verify
```

**Check hook is executable:**
```bash
ls -la .claude/hooks/
# Should show: -rwxr-xr-x block-no-verify.sh
```

**Check settings.json:**
```bash
cat .claude/settings.json
# Should reference the hook
```

### Push Rejected by Pre-push Hook

**Check what failed:**
```bash
cat ../your-repo-42/.git/hooks/pre-push.log
```

**Common issues:**
- Tests failing
- Missing Agent-Status trailer (use `agent-done`)
- Linter errors

### Session Times Out

Default timeout is 45 minutes. Adjust in config:

```yaml
agents:
  "agent:backend":
    timeout_minutes: 90  # More time for complex tasks
```

Or for complex issues, break them into smaller issues.

---

## Next Steps

Now that you have the basics working:

### 1. Customize Your Prompts

Tailor prompts to your project's conventions, testing framework, and style guide.

### 2. Set Up Code Review

Add a triage review agent that audits completed work:

```yaml
agents:
  "agent:triage-review":
    prompt: "prompts/triage-review.md"
    skip_review: true  # Don't review the reviewer
```

See `examples/prompts/triage-review.md` for a sample.

### 3. Add CI Integration

Run the orchestrator headlessly in CI:

```bash
issue-orchestrator start --no-dashboard --max-issues 10
```

### 4. Monitor with Logging

Enable debug logging for troubleshooting:

```bash
issue-orchestrator start --debug
tail -f ~/.issue-orchestrator.log
```

### 5. Explore Advanced Features

- **Milestones**: Focus on specific releases
- **Concurrency tuning**: Balance speed vs. resource usage
- **Custom commands**: Use different AI tools per agent
- **Multiple repos**: Run orchestrators for different projects

---

## Getting Help

- **Issues**: https://github.com/BruceBGordon/issue-orchestrator/issues
- **Discussions**: GitHub Discussions for questions
- **Protocol docs**: See `AGENT_PROTOCOL.md` for the agent contract
- **Hook docs**: See `HOOKS.md` for enforcement details

---

*Happy orchestrating!*
