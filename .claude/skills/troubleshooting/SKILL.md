---
name: troubleshooting
description: General orchestrator diagnostics, hook problems, performance issues, and infrastructure debugging. Use for "orchestrator behaving unexpectedly", hook hangs, iTerm2 slowness, or lock/state issues. For failed issues, use session-debugging skill instead.
---

# Troubleshooting

General diagnostics for orchestrator infrastructure issues.

## When to Use

- Orchestrator behaving unexpectedly
- Hooks hanging or failing
- iTerm2/terminal performance issues
- Lock or state problems
- General "something's wrong" scenarios

**For investigating failed issues/sessions, use the `session-debugging` skill instead.**

---

## Quick Diagnostics

```bash
# Check orchestrator status
issue-orchestrator status

# Check orchestrator log (filter out loop spam)
LOG=".issue-orchestrator/state/logs/orchestrator.log"
tail -f "$LOG" | grep -v "LOOP.*Iteration"

# See ONLY events
grep "\[EVENT\]" "$LOG" | tail -100

# See events + key transitions
grep -E "\[EVENT\]|\[STATE_MACHINE\]|Launched|Queued|review" "$LOG" | tail -100

# Check for errors
grep -i -E "error|exception|traceback" "$LOG" | tail -30

# List tmux sessions
tmux list-windows -t orchestrator

# Check web API (if running with --web-ui)
curl -s http://localhost:8080/api/status | jq
```

---

## Common Issues

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| Pre-push hook hangs | Infinite recursion in worktree | Check hook chain, use `GIT_TERMINAL_PROMPT=0` |
| iTerm2 very slow | Too many idle tabs | Run cleanup or restart orchestrator |
| Labels not applied | Wrong label_target (issue vs PR) | Check completion_processor logs |
| Sessions cycling/retry loop | `blocked-failed` label not added | Check `failed_this_cycle` mechanism |
| iTerm tabs exit immediately | Sandbox check failing | Check `_iterm2.py` sandbox_check |
| Orchestrator won't start | Lock file exists | Check `.issue-orchestrator/state/orchestrator.lock` |
| GitHub API errors | Rate limiting | Check `gh api rate_limit` |

---

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
```

### Expected Events

| Event | When | Key Data |
|-------|------|----------|
| `session.started` | Issue session launched | issue_number, session_id |
| `session.completed` | Session finished | issue_number, outcome, pr_url |
| `pr.created` | PR created | issue_number, pr_url |
| `review.queued` | PR queued for review | pr_number, issue_number |
| `review.started` | Review session launched | pr_number |

---

## Hook Debugging

### Pre-push Hook Hangs

If the pre-push hook hangs in worktrees:

```bash
# Check if it's a git prompt issue
GIT_TERMINAL_PROMPT=0 git push

# Check hook chain
cat .git/hooks/pre-push

# Look for recursive calls
grep -r "git push" .git/hooks/
```

**Common causes:**
- Hook calls `git push` which triggers the hook again
- Hook waits for user input (credential prompt)
- Hook runs validation that hangs

### Stop Hook Issues

The stop hook checks for agent-done marker:

```bash
# Check if marker exists
ls -la $WORKTREE/.agent-done-marker

# View marker content
cat $WORKTREE/.agent-done-marker
```

---

## Worktree Issues

### Missing .venv Symlink

If push fails with:
```
.venv/bin/lint-imports: No such file or directory
```

The worktree is missing its `.venv` symlink:

```bash
# Check if symlink exists
ls -la /path/to/worktree/.venv

# Should show -> /path/to/main/repo/.venv
```

**Fix:** `install_venv_symlink()` is called in `adapters/worktree/_worktree.py` during worktree creation AND reuse.

### Stale Completion Files

If sessions complete immediately with old data:

```bash
# Check completion files in worktree
ls -la /path/to/worktree/.issue-orchestrator/completion*.json

# View content (check timestamp)
cat /path/to/worktree/.issue-orchestrator/completion*.json | jq '.timestamp'
```

**Fix:** `Worktree.prepare_for_session()` in `control/worktree.py` should delete stale files.

---

## Performance Issues

### iTerm2 Slowness

Too many terminal tabs/windows degrade iTerm2 performance.

```bash
# Count orchestrator windows
tmux list-windows -t orchestrator | wc -l

# Kill all orchestrator tmux sessions
tmux kill-session -t orchestrator
```

**Prevention:** Configure `max_concurrent_sessions` in config to limit parallel sessions.

---

## Lock and State Issues

### Orchestrator Lock

If orchestrator won't start due to lock:

```bash
# Check lock file
cat .issue-orchestrator/state/orchestrator.lock

# Check if process is actually running
ps aux | grep issue-orchestrator

# If not running, remove stale lock
rm .issue-orchestrator/state/orchestrator.lock
```

### State Corruption

If state seems wrong:

```bash
# View current state
cat .issue-orchestrator/state/sessions.json | jq

# Check for orphaned sessions
tmux list-windows -t orchestrator -F "#{window_name}"
```

---

## Kill Stuck Processes

```bash
# Kill stuck orchestrators
pkill -f "issue-orchestrator.*start"

# Clean up stale tmux sessions
tmux kill-session -t orchestrator 2>/dev/null

# Kill specific e2e worker
pkill -f "e2e_worker"
```

---

## Key Files Reference

| File | Purpose |
|------|---------|
| `.issue-orchestrator/state/orchestrator.log` | Main orchestrator log |
| `.issue-orchestrator/state/sessions.json` | Current session state |
| `.issue-orchestrator/state/orchestrator.lock` | Lock file |
| `.issue-orchestrator/config/*.yaml` | Configuration files |
| `adapters/terminal/_iterm2.py` | iTerm2 adapter (sandbox check) |
| `control/session_launcher.py` | Session launch/completion handling |
