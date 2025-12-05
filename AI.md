# AI Assistant Guide for issue-orchestrator

This document helps AI assistants quickly understand the codebase.

## What This Project Does

**issue-orchestrator** orchestrates multiple Claude Code AI agents working on GitHub issues in parallel. It:

1. Fetches issues labeled for specific agents (e.g., `agent:web`, `agent:mobile`)
2. Creates isolated git worktrees per issue
3. Launches Claude Code sessions in tmux windows
4. Monitors sessions for completion, blocked state, or timeout
5. Manages concurrency (configurable N parallel sessions)
6. Provides a dashboard UI (display-only, see Known Issues)

## Architecture Overview

```
CLI (cli.py)
    └── Orchestrator (orchestrator.py) - Main async loop
            ├── Scheduler (scheduler.py) - Picks next issues by priority
            ├── Monitor (monitor.py) - Checks session status
            ├── TmuxManager (tmux.py) - Manages tmux sessions
            ├── Worktree (worktree.py) - Git worktree management
            ├── GitHub (github.py) - gh CLI wrapper
            └── Dashboard (dashboard.py) - Rich TUI (display only)
```

## Key Files

| File | Lines | Purpose |
|------|-------|---------|
| `orchestrator.py` | ~264 | Main orchestration loop, session lifecycle |
| `cli.py` | ~603 | CLI commands (start, status, pause, resume, etc.) |
| `dashboard.py` | ~168 | Rich TUI dashboard (DISPLAY ONLY - see Known Issues) |
| `tmux.py` | ~249 | TmuxManager for session/window management |
| `monitor.py` | ~218 | SessionMonitor - detects completion/blocked/timeout |
| `scheduler.py` | ~190 | Priority scheduling, dependency analysis |
| `analysis.py` | ~285 | Issue state analysis, branch detection |
| `worktree.py` | ~285 | Git worktree create/remove operations |
| `github.py` | ~300+ | Wrapper around `gh` CLI for issue/PR operations |
| `config.py` | ~129 | YAML config loading, AgentConfig |
| `models.py` | ~138 | Dataclasses: Issue, Session, SessionStatus, etc. |
| `locks.py` | ~150 | Lock claims to prevent concurrent work |

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
   - Create tmux window
   - Start Claude Code with prompt
   - Add in-progress label

4. handle_session_completion(session, status)
   - Remove in-progress label
   - If COMPLETED: cleanup worktree
   - If BLOCKED/NEEDS_HUMAN: leave for human review
   - Release lock
```

## Configuration (.issue-orchestrator.yaml)

```yaml
agents:
  "agent:web":
    prompt: ".issue-orchestrator/prompts/web.md"
    worktree_base: "../"
    timeout_minutes: 45
    model: sonnet

concurrency:
  max_sessions: 3
  session_timeout_minutes: 45

labels:
  in_progress: "in-progress"
  blocked: "blocked"
  needs_human: "needs-human"
  prefix: null  # Optional prefix for all labels

filter_label: null  # Only process issues with this label
filter_milestone: null  # Only process issues in this milestone
```

## Prompt Templates

Located in `examples/prompts/` or configured per-agent. Variables substituted:
- `{issue_number}` - GitHub issue number
- `{issue_title}` - Issue title

See Known Issues below for discussion of comment heading structure.

## Testing

**Current Status**: 414 tests, all passing

```bash
source .venv/bin/activate
pytest tests/unit/              # Run all tests
pytest tests/unit/ -v           # Verbose
pytest tests/unit/ --cov        # With coverage
```

**Test Structure:**
- All external calls (gh CLI, git, tmux, subprocess) are mocked
- Tests use pytest fixtures from `conftest.py`
- Coverage is good for core modules, gaps in dashboard/monitor

## Known Issues & TODOs

### 1. Dashboard Keyboard Input NOT IMPLEMENTED

**Problem**: The dashboard shows help bar with shortcuts `[q]uit [p]ause [r]esume [n]ext [1-9]attach` but **keyboard input is not captured**. The Rich `Live` context is display-only.

**Location**: `dashboard.py:140-152`

**To Fix**: Would need to:
- Add a key capture library (e.g., `pynput`, `blessed`, or `prompt_toolkit`)
- Create async input listener running alongside the display loop
- Wire key events to orchestrator methods

### 2. Prompt Template Comment Structure

**Issue**: Workers post comments on issues but the format needs correlation with CTO investigation comments.

**Suggested Enhancement**: Add configurable comment headings to `.issue-orchestrator.yaml`:

```yaml
comment_headings:
  implementation: "## Implementation"
  problems: "## Problems Encountered"
  pr_link: "## Pull Request"
  blocked_reason: "## Blocked: Reason"
  human_help: "## Needs Human: Question"
```

### 3. E2E Testing Gap

No end-to-end tests exist. Would require:
- Real tmux sessions (or comprehensive libtmux mocking)
- Test GitHub repo with real `gh` CLI
- Timing-based assertions (flaky by nature)

**Suggested Approach**: Integration tests that mock at GitHub API boundary but use real tmux/git.

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

## Dependencies

- Python 3.11+
- `gh` CLI (GitHub CLI, authenticated)
- `tmux`
- `git`
- `claude` CLI (Claude Code)
- `libtmux` (Python bindings)
- `rich` (TUI library)
- `pyyaml` (config parsing)

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
