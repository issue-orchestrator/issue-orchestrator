---
name: troubleshooting
description: Debug failing or stuck sessions, investigate orchestrator issues, diagnose hook problems, check locks. Use when sessions fail, hooks hang, iTerm2 is slow, or the orchestrator behaves unexpectedly.
---

# Troubleshooting

This skill provides context for debugging orchestrator issues.

## When to Use

- Debugging failing or stuck sessions
- Investigating orchestrator issues
- Diagnosing hook problems
- Checking session/lock state
- E2E test failures

## Key Resources

Read these files for context:
- [docs/development/debugging.md](docs/development/debugging.md) - Event system architecture and debugging commands
- [docs/development/TROUBLESHOOTING.md](docs/development/TROUBLESHOOTING.md) - Common issues and fixes

## Event System Architecture

Events flow through pluggy hooks:
```
TraceEvent(name, data) --> PluggyEventSink --> on_trace_event hook
                                                    |
                            +-----------------------+-----------------------+
                            |                       |                       |
                     LifecycleIPCPlugin    LifecycleSSEPlugin    LifecycleLoggingPlugin
                            |                       |                       |
                            v                       v                       v
                       IPC socket              SSE stream              Python logger
                                                                   (~/.issue-orchestrator.log)
```

## Quick Diagnostics

```bash
# Trace all log entries for a specific issue (since last startup)
issue-orchestrator trace 2641

# Alternative: use the shell script (from repo root)
./tools/trace-issue 2641

# Check orchestrator log - now in repo root
LOG=".issue-orchestrator/state/logs/orchestrator.log"

# Filter out loop spam
tail -f "$LOG" | grep -v "LOOP.*Iteration"

# See ONLY events
grep "\[EVENT\]" "$LOG" | tail -100

# See events + key transitions
grep -E "\[EVENT\]|\[STATE_MACHINE\]|Launched|Queued|review" "$LOG" | tail -100

# Check for errors
grep -i -E "error|exception|traceback" "$LOG" | tail -30

# Check orchestrator status
issue-orchestrator status

# List tmux sessions
tmux list-windows -t orchestrator

# Check web API (if running with --web-ui)
curl -s http://localhost:8080/api/status | jq

# View session output
issue-orchestrator output <issue_number>
```

## Issue-Specific Logging

All log entries for an issue are prefixed with `[issue-N]`. Use the trace command:

```bash
# Trace issue 2641 from the most recent orchestrator run
issue-orchestrator trace 2641

# Or use the shell script (from repo root)
./tools/trace-issue 2641

# Or manually:
grep "\[issue-2641\]" .issue-orchestrator/state/logs/orchestrator.log
```

## Completion File Debugging

Each agent writes to its own completion file:
```bash
# Check completion files in worktree
ls -la /tmp/e2e-worktrees/issue-orchestrator-*/.issue-orchestrator/

# View completion record
cat /tmp/e2e-worktrees/issue-orchestrator-{N}/.issue-orchestrator/completion-*.json | jq

# Check session log
tail -100 /tmp/e2e-worktrees/issue-orchestrator-{N}/.issue-orchestrator/session.log
```

## Common Issues

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| Sessions fail without completion | Prompt missing `agent-done` | Add agent-done instructions to prompt |
| Review not running | completion.json race condition | Check agent-specific completion files |
| Pre-push hook hangs | Infinite recursion in worktree | Check hook chain, see TROUBLESHOOTING.md |
| iTerm2 very slow | Too many idle tabs | Run cleanup or restart orchestrator |
| Labels not applied | Wrong label_target (issue vs PR) | Check completion_processor logs |
| Sessions cycling/retry loop | `blocked-failed` label not added | See "Session Retry Loop" below |
| iTerm tabs exit immediately | Sandbox check failing | Check `_iterm2.py` sandbox_check |
| Push fails with "No such file" | Missing .venv symlink in worktree | See "Worktree Issues" below |
| Stale completion detected | Old completion.json from previous session | Worktree not prepared properly |

## Worktree Issues

### Missing .venv Symlink

If push fails with errors like:
```
.venv/bin/lint-imports: No such file or directory
.venv/bin/pyright: No such file or directory
```

The worktree is missing its `.venv` symlink. Check:

```bash
# Check if symlink exists
ls -la /path/to/worktree/.venv

# Should show -> /path/to/main/repo/.venv
# If missing, the worktree reuse code didn't call install_venv_symlink()
```

Fix: `install_venv_symlink()` is called in `adapters/worktree/_worktree.py` during worktree creation AND reuse.

### Stale Completion Files

If sessions complete immediately with old data, stale `completion.json` from a previous session is being read.

```bash
# Check completion files in worktree
ls -la /path/to/worktree/.issue-orchestrator/completion*.json

# View completion content
cat /path/to/worktree/.issue-orchestrator/completion*.json | jq
```

Fix: `Worktree.prepare_for_session()` in `control/worktree.py` deletes stale completion files before session launch.

## Session Retry Loop Debugging

**Symptom:** Sessions keep starting, failing quickly (~10-20 seconds), and restarting in a loop.

**Root cause options:**
1. `blocked-failed` label not being added after failure
2. `blocked-*` labels not being filtered by audit
3. Sessions failing before Claude even starts (sandbox check, etc.)
4. Race condition: planner uses stale cache before `blocked-failed` label is visible

**Prevention mechanism:**
The orchestrator now tracks `failed_this_cycle` in state - a set of issue numbers that failed since the last cache refresh. The planner skips these issues, preventing immediate retry. Look for:

```bash
grep "failed_this_cycle" ~/.issue-orchestrator.log | tail -20
```

Expected flow:
```
[COMPLETION] Issue #123 added to failed_this_cycle (prevents retry until cache refresh)
[REFRESH] Clearing failed_this_cycle: {123} (labels now synced from GitHub)
```

### Step 1: Check if sessions are failing

```bash
# Look for rapid session failures
tail -200 .issue-orchestrator/state/logs/orchestrator.log | grep -E "(FAILED|failed|Session not running)"
```

Expected to see:
```
Session not running: issue=2641 ... completion=... exists=False
[TRANSITION] issue #2641: ACTIVE → FAILED (runtime=0min)
```

### Step 2: Check if blocked-failed label is being added

```bash
# Look for label operations after failure
tail -500 .issue-orchestrator/state/logs/orchestrator.log | grep -iE "(blocked-failed|AddLabel|COMPLETION.*action)"
```

**If you see `[COMPLETION] Applying N actions`** → Actions are generated, check if they're applied
**If you see `[COMPLETION] No actions generated`** → Bug in `completion_handler.generate_completion_actions()`
**If you see neither** → `handle_session_completion` not being called

### Step 3: Trace the completion flow

The flow for failed sessions:
```
Observer detects session terminated
    ↓
session_controller.decide_outcome() → SessionStatus.FAILED
    ↓
handle_session_completion() called
    ↓
completion_handler.process_completion(session, status)
    ↓
completion_handler.generate_completion_actions(session, status)
    → Returns [AddLabelAction(blocked-failed), AddCommentAction, RemoveLabelAction(in-progress)]
    ↓
action_applier.apply_all(actions)
    → Calls repository_host.add_label(), etc.
```

### Step 4: Check the audit filter

If labels ARE being added but issues still retry:

```bash
# Check if audit is filtering blocked issues
tail -200 .issue-orchestrator/state/logs/orchestrator.log | grep -iE "(audit|skip|blocked)"
```

The audit should skip issues with ANY `blocked-*` prefix label. Check `infra/audit.py`:
```python
# This should use label_utils.is_blocking_any() or get_blocking_labels()
blocking_labels = label_utils.get_blocking_labels(issue.labels)
if blocking_labels:
    return IssueAuditEntry(issue, SkipReason.BLOCKED, f"label: {blocking_labels[0]}")
```

### Step 5: Check why Claude exits quickly

If sessions fail in <20 seconds, Claude may not even be starting:

```bash
# Check iTerm sandbox check
grep -A5 "sandbox_check" src/issue_orchestrator/adapters/terminal/_iterm2.py
```

Common causes:
- Sandbox verification command fails (exit code confusion)
- Shell command composition errors
- Missing environment variables

### Key Files for Retry Loop Issues

| File | What to check |
|------|---------------|
| `control/session_launcher.py:handle_session_completion()` | Are actions being applied? |
| `control/completion_handler.py:generate_completion_actions()` | Are actions being generated for FAILED status? |
| `control/action_applier.py:apply_all()` | Are actions being executed? |
| `infra/audit.py:audit_issue()` | Is `blocked-*` filtering working? |
| `infra/labels.py:get_blocking_labels()` | Does it match all `blocked-*` prefixes? |
| `adapters/terminal/_iterm2.py` | Is sandbox check aborting sessions? |

### Adding Diagnostic Logging

If the flow isn't clear, add logging:

```python
# In session_launcher.py handle_session_completion():
if result.actions:
    logger.info(
        "[COMPLETION] Applying %d actions for issue #%d status=%s: %s",
        len(result.actions), session.issue.number, status.value,
        [type(a).__name__ for a in result.actions],
    )
else:
    logger.warning(
        "[COMPLETION] No actions generated for issue #%d status=%s",
        session.issue.number, status.value,
    )
```

## Expected Events

When debugging, check these events are being logged:

| Event | When | Key Data |
|-------|------|----------|
| `session.started` | Issue session launched | issue_number, session_id |
| `session.completed` | Session finished | issue_number, outcome, pr_url |
| `pr.created` | PR created | issue_number, pr_url |
| `review.queued` | PR queued for review | pr_number, issue_number |
| `review.started` | Review session launched | pr_number |

## Claude Session Logs

Find logs for debugging agent behavior:
```bash
WORKTREE="/path/to/worktree"
ESCAPED=$(echo "$WORKTREE" | sed 's|^/|-|' | tr '/' '-')
ls -la ~/.claude/projects/$ESCAPED/
```

## Automatic Failure Context (NEW)

When sessions fail, the orchestrator now **automatically surfaces AI session logs**. Look for `[FAILURE_CONTEXT]` in the orchestrator log:

```bash
# See automatic failure analysis
grep "\[FAILURE_CONTEXT\]" ~/.issue-orchestrator.log | tail -50
```

The failure context includes:
- **Errors found**: Tool errors, API errors, etc.
- **Permission issues**: If Claude was blocked by permission prompts (suggests using `bypassPermissions`)
- **Recent activity**: Last few tool calls before failure
- **Agent-done status**: Whether the agent called agent-done before exiting

This is AI-system agnostic - works for Claude, Codex, Gemini, etc. via the `SessionLogProvider` protocol.

## Claude Session Log Debugging

When sessions fail quickly, check Claude's logs to see what happened:

```bash
# Find Claude project dir for a worktree
WORKTREE="/Users/brucegordon/dev/issue-orchestrator-2641"
ESCAPED=$(echo "$WORKTREE" | sed 's|^/|-|' | tr '/' '-')
ls -la ~/.claude/projects/$ESCAPED/

# View most recent session (sorted by date)
ls -lt ~/.claude/projects/$ESCAPED/*.jsonl | head -1

# Check session contents
cat ~/.claude/projects/$ESCAPED/<session-id>.jsonl | head -50

# Look for errors in session
cat ~/.claude/projects/$ESCAPED/<session-id>.jsonl | grep -i error

# Check if agent-done was called
cat ~/.claude/projects/$ESCAPED/<session-id>.jsonl | grep -i agent-done
```

**What to look for:**
- Permission errors (can't read files outside worktree)
- Tool errors (Task agent failures)
- Session ends abruptly without agent-done
- agent-done called with blocked/needs_human

**Common Claude session issues:**

| Symptom | Cause | Fix |
|---------|-------|-----|
| Permission error reading main repo | Worktree isolation | Prompt should reference worktree paths |
| Session log very short (<10 entries) | Killed early | Check orchestrator timeout settings |
| No agent-done in log | Session didn't complete | Check if Claude got stuck or errored |
| Task agent spawned then nothing | Subagent timeout | Check Task tool configuration |

## Kill Stuck Processes

```bash
# Kill stuck orchestrators
pkill -f "issue-orchestrator.*start"

# Clean up stale tmux sessions
tmux kill-session -t orchestrator 2>/dev/null
```
