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
7. Orchestrates code reviews and CTO batch reviews

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
| `orchestrator.py` | Main orchestration loop, session lifecycle, review workflow |
| `cli.py` | CLI commands (start, status, pause, resume, etc.) |
| `agent_done.py` | `agent-done` CLI - enforced completion command |
| `worktree.py` | Git worktree create/remove + pre-push hook installation |
| `web.py` | FastAPI web dashboard with real-time updates, auto port handling |
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
   - Scan for PRs needing code review (label-based recovery)

2. run_loop() (every 10 seconds)
   - Monitor active sessions for completion
   - Handle completions (cleanup worktree, update labels)
   - Process pending code reviews
   - If capacity available, launch new sessions

3. launch_session(issue)
   - Claim lock
   - Create worktree with issue branch
   - Run setup_worktree commands (e.g., npm ci)
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
   - If COMPLETED: cleanup worktree, queue code review
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
  - Also scanned at startup for crash recovery
  - Review agent checks code quality, tests
  - Uses: agent-done approved --summary "..." OR agent-done changes_requested --issues "..."
  - Label: "needs-code-review" → "code-reviewed" (approved) or "needs-rework" (changes_requested)
       ↓
     /   \
Approved  Changes Requested
    |           ↓
    |     [Rework Loop] (automatic, up to max_rework_cycles)
    |       - Orchestrator detects "needs-rework" label
    |       - Re-queues work agent to fix issues
    |       - Tracks cycle via "rework-1", "rework-2" labels
    |       - After max cycles: escalates to "needs-human"
    |           ↓
    |     Back to Code Review
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

  # Rework iteration limit
  max_rework_cycles: 2  # Escalate to needs-human after N rework cycles

  # Stage 2: CTO Batch Review
  cto_review_agent: "agent:cto"
  cto_reviewed_label: "cto-reviewed"
  cto_review_threshold: 5  # Trigger after 5 PRs

labels:
  needs_rework: "needs-rework"  # Label for PRs needing rework after review
```

### Orchestrator Methods (orchestrator.py)

| Method | Purpose |
|--------|---------|
| `queue_code_review()` | Queue PR for code review (called on work completion) |
| `launch_review_session()` | Launch a code review agent for a PR |
| `process_pending_reviews()` | Process queued reviews (called each loop) |
| `scan_needs_rework_prs()` | Scan for PRs with needs-rework label |
| `launch_rework_session()` | Launch work agent to fix review issues |
| `process_pending_reworks()` | Process queued reworks (called each loop) |
| `check_cto_review_trigger()` | Check if CTO batch review should be triggered |

### UI Phase Detection (web.py)

The dashboard shows whether a session is "Coding" or "Reviewing" based on the tmux session name:
- Tmux sessions starting with `issue-*` → "Coding" phase
- Tmux sessions starting with `review-*` → "Reviewing" phase

```python
tmux_name = session.tmux_session_name or ""
is_review = tmux_name.startswith("review-")
phase = "Reviewing" if is_review else "Coding"
```

## The `agent-done` Command

This is the **enforced** way for agents to complete work. Direct `git push` is blocked by pre-push hooks.

**Location**: `agent_done.py`

**Usage**:
```bash
# Work agent - successful completion
agent-done completed --implementation "What was done" --problems "None"

# Work agent - blocked
agent-done blocked --reason "Why" --attempted "What was tried"

# Work agent - need human input
agent-done needs_human --question "Question for human"

# Reviewer agent - approve PR
agent-done approved --summary "Code is clean, tests pass"

# Reviewer agent - request changes
agent-done changes_requested --issues "Missing tests for X, error handling in Y"
```

**What it does**:
1. Validates required fields for the status
2. Adds `Agent-Status:` trailers to last commit
3. Pushes code (pre-push hook validates trailers)
4. Creates PR (for completions)
5. Posts structured comment on issue/PR
6. Adds appropriate labels:
   - `blocked`, `needs-human` (work agent issues)
   - `code-reviewed` (reviewer approved), `needs-rework` (reviewer requests changes)

## Pre-push Hook Enforcement

**Location**: `hooks/pre-push`

When `enforce_hooks: true` in config, the orchestrator installs a pre-push hook that:
- Checks for `Agent-Status:` trailer in latest commit
- Validates required fields based on status type
- Blocks push if validation fails

This forces agents to use `agent-done` instead of pushing directly.

## Agent Guardrails (Defense in Depth)

The orchestrator uses multiple layers to ensure agents use `agent-done` instead of directly creating PRs:

### Layer 1: `gh pr create` Interception

**Location**: `scripts/gh`

A wrapper script intercepts all `gh` commands in agent sessions:
- If `gh pr create` is called without `ORCHESTRATOR_GH_AUTH=agent-done-authorized`, it blocks with an error
- All other `gh` commands pass through normally
- `agent-done` sets the auth env var, so it can still create PRs

**How it works:**
- Session launchers (iTerm2, tmux) prepend `scripts/` to PATH
- The wrapper shadows the real `gh` command
- Only `agent-done` knows the auth token

**Error shown to agents:**
```
╔════════════════════════════════════════════════════════════════╗
║  ❌ ERROR: Direct 'gh pr create' is BLOCKED                    ║
╠════════════════════════════════════════════════════════════════╣
║  You MUST use 'agent-done' to create PRs.                      ║
║  ✅ Correct usage:                                             ║
║     agent-done completed \                                     ║
║       --implementation "What you implemented" \                ║
║       --problems "None"                                        ║
╚════════════════════════════════════════════════════════════════╝
```

### Layer 2: PR Verification Tokens

**Location**: `agent_done.py`

PRs created via `agent-done` include a hidden verification marker:
```html
<!-- orchestrator-verified:a1b2c3d4e5f6g7h8 -->
```

The token is a truncated SHA-256 hash of `issue_number + secret`. The secret is configurable via `ORCHESTRATOR_PR_SECRET` env var.

**What the orchestrator checks:**
- At startup and when PRs are queued, checks for the marker
- PRs without markers are logged as warnings
- Helps detect any PRs that slipped through the wrapper

### Layer 3: Orchestrator Logging

PRs without verification tokens are logged:
```
PR #123: ⚠️  No verification token - created outside agent-done
```

This creates an audit trail for PRs created incorrectly.

### Testing the Wrapper

```bash
# Test blocking (should fail with error box)
export PATH="/path/to/issue-orchestrator/src/issue_orchestrator/scripts:$PATH"
gh pr create --title "test" --body "test"

# Test authorized (should pass through)
export ORCHESTRATOR_GH_AUTH="agent-done-authorized"
gh pr create --title "test" --body "test"

# Other commands work normally
unset ORCHESTRATOR_GH_AUTH
gh pr list
```

## Configuration (.issue-orchestrator.yaml)

```yaml
repo: owner/repo  # Optional, defaults to git remote

agents:
  "agent:web":
    prompt: ".issue-orchestrator/prompts/web.md"
    worktree_base: "../"
    timeout_minutes: 45
    model: sonnet
    permission_mode: bypassPermissions  # or default
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

# Limits and API behavior
max_issues_to_start: 0  # Max issues to start this run (0 = unlimited)
issue_fetch_limit: 100  # Max issues to fetch from GitHub API (default: 100)
queue_refresh_seconds: 600  # Web UI GitHub refresh interval (0 = manual only)

# Milestone sorting
milestone_sort: "due_date"  # or "number" - how to prioritize milestones

# Worktree setup commands (run after creating worktree)
setup_worktree:
  - "cd src/frontend && npm ci --silent"
  - "pip install -r requirements.txt"

labels:
  in_progress: "in-progress"
  blocked: "blocked"
  needs_human: "needs-human"
  prefix: "bot"  # Optional: "bot:in-progress", etc.

# UI mode
ui_mode: "tmux"  # or "iterm2" or "web"
web_port: 8080   # Port for web dashboard

# Hook enforcement
enforce_hooks: true
# pre_push_hook: "./custom-hook.sh"  # Optional custom hook

# Tab cleanup
close_completed_tabs: true
close_failed_tabs: false

# Filtering
filter_label: null
filter_milestone: null
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
- FastAPI server on port 8080 (configurable)
- Real-time dashboard in browser
- REST API for status and control
- **Auto port handling**: If port is in use, kills existing process automatically
- Start with: `issue-orchestrator start --ui-mode web`

## CLI Options

Key `start` command options:
```bash
issue-orchestrator start \
  --ui-mode web \           # tmux, iterm2, or web
  --port 8080 \             # Web dashboard port
  --config /path/to/config.yaml \  # Custom config file path
  --milestone "Sprint 1" \  # Filter by milestone (name or number)
  --max-issues 10           # Limit issues to start this run
```

## Prompt Templates

Located in `examples/prompts/`. Variables substituted:
- `{issue_number}` - GitHub issue number
- `{issue_title}` - Issue title
- `{worktree}` - Path to the worktree

Prompts MUST instruct agents to use `agent-done` for completion.

## Testing

```bash
source .venv/bin/activate
pytest tests/unit/              # Run all tests (~917 tests)
pytest tests/unit/ -v           # Verbose
pytest tests/unit/ --cov        # With coverage (90%+)
```

**Test mode**: Use `--test-mode` flag to run with mock data:
```bash
issue-orchestrator start --test-mode
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
curl -s http://localhost:8080/api/status | python3 -m json.tool
curl -s http://localhost:8080/api/state | python3 -m json.tool
curl -s http://localhost:8080/api/config | python3 -m json.tool
```

**Debug pending reviews:**
```bash
curl -s http://localhost:8080/api/state | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('Pending reviews:', len(d.get('pending_reviews', [])))
for p in d.get('pending_reviews', []):
    print(f'  PR #{p[\"pr_number\"]}: {p[\"branch_name\"]}')"
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
with patch('issue_orchestrator.github._run_gh') as mock_gh:
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
│   ├── scripts/
│   │   └── gh             # gh wrapper (blocks unauthorized pr create)
│   ├── iterm2.py          # iTerm2 manager
│   ├── locks.py           # Lock management
│   ├── models.py          # Data models
│   ├── monitor.py         # Session monitoring
│   ├── orchestrator.py    # Main orchestrator
│   ├── scheduler.py       # Issue scheduling
│   ├── setup_wizard.py    # Interactive setup
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

## Web Dashboard Features

### Port Conflict Handling

The web dashboard automatically handles port conflicts:
- On startup, checks if the configured port is in use
- If in use, automatically kills the blocking process using `lsof`
- Logs the action and continues startup
- Falls back to error with helpful message if kill fails

### API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Dashboard HTML |
| `/api/status` | GET | Current orchestrator status |
| `/api/state` | GET | Full state including pending reviews |
| `/api/config` | GET | Current configuration |
| `/api/pause` | POST | Pause orchestrator |
| `/api/resume` | POST | Resume orchestrator |
| `/api/shutdown` | POST | Graceful shutdown |
| `/api/refresh` | POST | Refresh issue queue from GitHub |
| `/api/events` | GET | SSE stream for real-time updates |

### Real-time Updates

The dashboard uses Server-Sent Events (SSE) for real-time updates:
- Session status changes
- New sessions starting
- Completions and failures
- Queue changes

## Troubleshooting

### Sessions Failing Without Completion

**Symptom:** Sessions end with "without completion markers", marked as FAILED.

**Causes:**
1. Agent prompt doesn't include `agent-done` instructions
2. Pre-push hook blocking push (agent can't complete)
3. Agent crashing/timeout before completion

**Fix:** Ensure agent prompts include `agent-done` usage in "When Done" section.

### Pre-Push Hook Infinite Recursion

**Symptom:** Push hangs forever, hook log shows repeated "Pre-push hook started".

**Cause:** When worktrees are reused, `install_hooks()` was reading `core.hooksPath` from
worktree config (which has our override), then copying the chained wrapper as the "project hook".

**Fix:** Code now reads `core.hooksPath` from main repo config only. To repair existing worktrees:
```bash
# Copy original project hook to all worktrees
MAIN_HOOK="/path/to/repo/.githooks/pre-push"
for dir in /path/to/repo-*/; do
  HOOKS_DIR="/path/to/repo/.git/worktrees/$(basename $dir)/hooks"
  if grep -q "Chained pre-push" "$HOOKS_DIR/pre-push.project" 2>/dev/null; then
    cp "$MAIN_HOOK" "$HOOKS_DIR/pre-push.project"
  fi
done
```

### Main Repo hooksPath Corrupted

**Symptom:** Pushes from main repo fail, `git config core.hooksPath` shows worktree path.

**Fix:**
```bash
cd /path/to/main/repo
git config --unset core.hooksPath
git config core.hooksPath .githooks
```

### iTerm2 Slowdown

**Symptom:** Creating new tabs takes 30-60+ seconds.

**Cause:** Too many accumulated idle tabs from previous sessions.

**Fix:** Clean up idle tabs:
```bash
python -c "from issue_orchestrator.iterm2 import cleanup_idle_tabs; cleanup_idle_tabs()"
```
Or restart orchestrator (cleanup runs at startup for iTerm2/web modes).

### Missing Labels

**Symptom:** Warnings about labels not found (e.g., "failed" label).

**Fix:** Create missing labels in the repo:
```bash
gh label create "failed" -R owner/repo --description "Agent session failed" --color "B60205"
```

### Lock Cleanup

Locks are stored in `/tmp/issue-orchestrator/locks/`. Cleanup runs at startup:
- Stale locks (>60 min old) are removed
- Orphaned locks (no active session) are removed
- Both `issue-*` and `review-*` prefixes are cleaned

Manual cleanup:
```bash
rm -rf /tmp/issue-orchestrator/locks/*
```
