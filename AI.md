# AI Assistant Guide for issue-orchestrator

This document helps AI assistants quickly understand the codebase.

## What This Project Does

**issue-orchestrator** orchestrates multiple Claude Code AI agents working on GitHub issues in parallel. It:

1. Fetches issues labeled for specific agents (e.g., `agent:web`, `agent:mobile`)
2. Creates isolated git worktrees per issue
3. Launches Claude Code sessions in tmux, iTerm2 tabs, or web dashboard
4. Installs pre-push hooks to enforce structured completion via `agent-done`
5. Monitors sessions for completion, blocked state, or timeout
6. Manages concurrency (configurable N parallel sessions)

## Architecture Overview

```
CLI (cli.py)
    └── Orchestrator (orchestrator.py) - Main async loop
            ├── Scheduler (scheduler.py) - Picks next issues by priority
            ├── Monitor (monitor.py) - Checks session status
            ├── TmuxManager (tmux.py) - Manages tmux sessions
            ├── ITerm2Manager (iterm2.py) - Manages iTerm2 tabs (macOS)
            ├── WebDashboard (web.py) - FastAPI web dashboard
            ├── Worktree (worktree.py) - Git worktree + hook management
            ├── GitHub (github.py) - gh CLI wrapper
            ├── AgentDone (agent_done.py) - CLI for agent completion
            └── Dashboard (dashboard.py) - Rich TUI (display only)
```

## Key Files

| File | Purpose |
|------|---------|
| `orchestrator.py` | Main orchestration loop, session lifecycle |
| `cli.py` | CLI commands (start, status, pause, resume, etc.) |
| `agent_done.py` | `agent-done` CLI - enforced completion command |
| `worktree.py` | Git worktree create/remove + pre-push hook installation |
| `web.py` | FastAPI web dashboard with real-time updates |
| `iterm2.py` | iTerm2 tab management (macOS) |
| `tmux.py` | TmuxManager for session/window management |
| `monitor.py` | SessionMonitor - detects completion/blocked/timeout |
| `scheduler.py` | Priority scheduling, dependency analysis |
| `analysis.py` | Issue state analysis, branch detection |
| `github.py` | Wrapper around `gh` CLI for issue/PR operations |
| `config.py` | YAML config loading |
| `models.py` | Dataclasses: Issue, Session, SessionStatus, etc. |
| `hooks/pre-push` | Pre-push hook script (validates agent-done trailers) |

## Core Workflow

```
1. startup()
   - Clean stale in-progress labels
   - Release old locks

2. run_loop() (every 10 seconds)
   - Monitor active sessions for completion
   - Handle completions (cleanup worktree, update labels)
   - If capacity available, launch new sessions

3. launch_session(issue)
   - Claim lock
   - Create worktree with issue branch
   - Install pre-push hook (if enforce_hooks: true)
   - Create tmux window / iTerm2 tab / web session
   - Start Claude Code with prompt
   - Add in-progress label

4. Agent completes via agent-done
   - agent-done adds trailers to commit
   - Pre-push hook validates trailers
   - Push, create PR, post comment

5. handle_session_completion(session, status)
   - Remove in-progress label
   - If COMPLETED: cleanup worktree, trigger code review
   - If BLOCKED/NEEDS_HUMAN: leave for human review
   - Release lock

6. Code Review (if configured)
   - PR gets labeled "needs-code-review"
   - Orchestrator launches review agent for that PR
   - Review agent approves or requests changes
   - Label flipped to "code-reviewed"

7. CTO Batch Review (if threshold reached)
   - Orchestrator counts "code-reviewed" PRs
   - When threshold reached, creates issue for CTO agent
   - CTO reviews patterns across multiple PRs
```

## Two-Stage Review Workflow

The orchestrator supports a two-stage review pipeline:

```
Work Agent creates PR
       ↓
[Stage 1: Code Review] (per-PR, immediate)
  - Triggered immediately by orchestrator
  - Review agent checks code quality, tests
  - Approves or requests changes
  - Label: "needs-code-review" → "code-reviewed"
       ↓
Humans can optionally review on GitHub
       ↓
[Stage 2: CTO Batch Review] (batch, threshold-triggered)
  - Triggered when N code-reviewed PRs accumulate
  - CTO agent reviews patterns across PRs
  - Label: "code-reviewed" → "cto-reviewed"
       ↓
Manual merge
```

### Key Design Decisions

1. **Orchestrator manages workflow** - Agents are workers with simple, focused jobs. The orchestrator triggers the right agent at the right time.

2. **Two trigger modes**:
   - **Immediate (in-memory)**: When work agent completes, orchestrator immediately queues code review
   - **Recovery (label-based)**: On startup, scans GitHub for PRs with `needs-code-review` label

3. **Labels as source of truth** - Crash-safe: labels persist, orchestrator picks up where it left off

### Review Configuration

```yaml
review:
  # Stage 1: Code Review (per-PR)
  code_review_agent: "agent:reviewer"
  code_review_label: "needs-code-review"
  code_reviewed_label: "code-reviewed"

  # Stage 2: CTO Batch Review
  cto_review_agent: "agent:cto"
  cto_reviewed_label: "cto-reviewed"
  cto_review_threshold: 5  # Trigger after 5 PRs
```

### Orchestrator Methods (orchestrator.py)

| Method | Line | Purpose |
|--------|------|---------|
| `queue_code_review()` | 564 | Queue PR for code review (called on work completion) |
| `launch_review_session()` | 594 | Launch a code review agent for a PR |
| `process_pending_reviews()` | 662 | Process queued reviews (called each loop) |
| `check_cto_review_trigger()` | 687 | Check if CTO batch review should be triggered |

### UI Phase Detection (web.py:115-118)

The dashboard shows whether a session is "Coding" or "Reviewing" based on the tmux session name:
- Tmux sessions starting with `issue-*` → "Coding" phase
- Tmux sessions starting with `review-*` → "Reviewing" phase

```python
tmux_name = session.tmux_session_name or ""
is_review = tmux_name.startswith("review-")
phase = "Reviewing" if is_review else "Coding"
```

### Setup Wizard Defaults

The setup wizard (`setup_wizard.py`) defaults to enabling the review workflow (opt-out):
- New projects: Line 476 - `default=True` for Stage 1 review
- Existing projects: Line 760 - offers to add review if not present

## The `agent-done` Command

This is the **enforced** way for agents to complete work. Direct `git push` is blocked by pre-push hooks.

**Location**: `agent_done.py`

**Usage**:
```bash
# Successful completion
agent-done completed --implementation "What was done" --problems "None"

# Blocked
agent-done blocked --reason "Why" --attempted "What was tried"

# Need human input
agent-done needs_human --question "Question for human"
```

**What it does**:
1. Validates required fields for the status
2. Adds `Agent-Status:` trailers to last commit
3. Pushes code (pre-push hook validates trailers)
4. Creates PR (for completions)
5. Posts structured comment on issue
6. Adds appropriate labels (blocked, needs-human)

## Pre-push Hook Enforcement

**Location**: `hooks/pre-push`

When `enforce_hooks: true` in config, the orchestrator installs a pre-push hook that:
- Checks for `Agent-Status:` trailer in latest commit
- Validates required fields based on status type
- Blocks push if validation fails

This forces agents to use `agent-done` instead of pushing directly.

## Configuration (.issue-orchestrator.yaml)

```yaml
agents:
  "agent:web":
    prompt: ".issue-orchestrator/prompts/web.md"
    worktree_base: "../"
    timeout_minutes: 45
    model: sonnet
    # command: "claude --model {model} '{initial_prompt}'"  # Custom

  # Multi-repo example: mobile agent works on different repo
  "agent:mobile":
    prompt: ".issue-orchestrator/prompts/mobile.md"
    repo_root: "/path/to/mobile-app"  # Different repo
    timeout_minutes: 60

  # Non-code agent: skip code review
  "agent:domain-expert":
    prompt: ".issue-orchestrator/prompts/domain-expert.md"
    skip_review: true  # Skip code review for non-code agents

concurrency:
  max_concurrent_sessions: 3
  session_timeout_minutes: 45

# Limits
max_issues_to_start: 0  # Max issues to start this run (0 = unlimited)
queue_refresh_seconds: 600  # Web UI GitHub refresh interval (0 = manual only)

labels:
  in_progress: "in-progress"
  blocked: "blocked"
  needs_human: "needs-human"
  prefix: "bot"  # Optional: "bot:in-progress", etc.

# UI mode
ui_mode: "tmux"  # or "iterm2" or "web"

# Hook enforcement
enforce_hooks: true
# pre_push_hook: "./custom-hook.sh"  # Optional custom hook

# Tab cleanup
close_completed_tabs: true
close_failed_tabs: false

# Filtering
filter_label: null
filter_milestone: null

# Comment headings (customizable)
comment_headings:
  implementation: "## Implementation"
  problems: "## Problems Encountered"
  pr_link: "## Pull Request"
  blocked: "## Blocked"
  needs_human: "## Needs Human Input"
```

## UI Modes

### tmux (default)
- Uses a tmux session named `orchestrator`
- Each issue gets a window: `issue-{number}`
- Attach with: `issue-orchestrator attach <issue>` or `tmux attach`

### iterm2 (macOS)
- Uses AppleScript to control iTerm2
- Each issue gets a tab
- Requires iTerm2 to be running

### web
- FastAPI server on port 8080
- Real-time dashboard in browser
- REST API for status and control
- Start with: `issue-orchestrator start --ui-mode web`

## Prompt Templates

Located in `examples/prompts/`. Variables substituted:
- `{issue_number}` - GitHub issue number
- `{issue_title}` - Issue title
- `{worktree}` - Path to the worktree

Prompts MUST instruct agents to use `agent-done` for completion.

## Testing

```bash
source .venv/bin/activate
pytest tests/unit/              # Run all tests (~15s, 771 tests)
pytest tests/unit/ -v           # Verbose
pytest tests/unit/ --cov        # With coverage (90%+)
```

**Test mode**: Use `--test-mode` flag to run with mock data:
```bash
issue-orchestrator start --test-mode
```

## CLI Options

Key `start` command options:
```bash
issue-orchestrator start \
  --ui-mode web \           # tmux, iterm2, or web
  --port 8080 \             # Web dashboard port
  --milestone "Sprint 1" \  # Filter by milestone (name or number)
  --max-issues 10           # Limit issues to start this run
```

## Quick Debugging

**Check what's running:**
```bash
issue-orchestrator status
tmux list-windows -t orchestrator
```

**See session output:**
```bash
issue-orchestrator output <issue_number>
tmux capture-pane -t orchestrator:issue-<number> -p
```

**Attach to session:**
```bash
issue-orchestrator attach <issue_number>
# or
tmux attach -t orchestrator:issue-<number>
```

**Check web dashboard:**
```bash
curl http://localhost:8080/api/status
```

## Dependencies

- Python 3.11+
- `gh` CLI (GitHub CLI, authenticated)
- `tmux` (for tmux mode)
- `git`
- `claude` CLI (Claude Code)
- `libtmux` (Python tmux bindings)
- `rich` (TUI library)
- `pyyaml` (config parsing)
- `fastapi` + `uvicorn` (web dashboard)
- `jinja2` (HTML templates)

## Common Patterns

**Patching for tests:**
```python
with patch('issue_orchestrator.github.run_gh_command') as mock_gh:
    mock_gh.return_value = '{"data": ...}'
    # test code
```

**The singleton TmuxManager:**
```python
from issue_orchestrator.tmux import get_manager
manager = get_manager()  # Returns global _manager singleton
```

**Async orchestrator methods:**
```python
orchestrator = Orchestrator(config)
await orchestrator.startup()
await orchestrator.run_loop()  # Runs until shutdown
```

## File Structure

```
issue-orchestrator/
├── src/issue_orchestrator/
│   ├── __init__.py
│   ├── agent_done.py      # agent-done CLI
│   ├── analysis.py        # Issue analysis
│   ├── cli.py             # Main CLI
│   ├── config.py          # Config loading
│   ├── dashboard.py       # Rich TUI
│   ├── github.py          # GitHub API via gh
│   ├── hooks/
│   │   └── pre-push       # Pre-push hook script
│   ├── iterm2.py          # iTerm2 manager
│   ├── locks.py           # Lock management
│   ├── models.py          # Data models
│   ├── monitor.py         # Session monitoring
│   ├── orchestrator.py    # Main orchestrator
│   ├── scheduler.py       # Issue scheduling
│   ├── templates/
│   │   └── dashboard.html # Web dashboard template
│   ├── test_data.py       # Mock data for testing
│   ├── tmux.py            # Tmux manager
│   ├── web.py             # FastAPI web dashboard
│   └── worktree.py        # Git worktree management
├── examples/
│   ├── config.example.yaml
│   └── prompts/
│       ├── simple-fix.md
│       ├── feature.md
│       └── minimal-test.md
├── tests/
│   └── unit/
├── README.md
├── AGENT_PROTOCOL.md
├── AI.md                  # This file
└── pyproject.toml
```
