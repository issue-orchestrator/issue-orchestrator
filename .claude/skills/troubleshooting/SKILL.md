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
- [docs/DEBUGGING.md](docs/DEBUGGING.md) - Event system architecture and debugging commands
- [docs/ai/TROUBLESHOOTING.md](docs/ai/TROUBLESHOOTING.md) - Common issues and fixes

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
# Check orchestrator log (filter out loop spam)
tail -f ~/.issue-orchestrator.log | grep -v "LOOP.*Iteration"

# See ONLY events
grep "\[EVENT\]" ~/.issue-orchestrator.log | tail -100

# See events + key transitions
grep -E "\[EVENT\]|\[STATE_MACHINE\]|Launched|Queued|review" ~/.issue-orchestrator.log | tail -100

# Check for errors
grep -i -E "error|exception|traceback" ~/.issue-orchestrator.log | tail -30

# Check orchestrator status
issue-orchestrator status

# List tmux sessions
tmux list-windows -t orchestrator

# Check web API (if running with --web-ui)
curl -s http://localhost:8080/api/status | jq

# View session output
issue-orchestrator output <issue_number>
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

## Kill Stuck Processes

```bash
# Kill stuck orchestrators
pkill -f "issue-orchestrator.*start"

# Clean up stale tmux sessions
tmux kill-session -t orchestrator 2>/dev/null
```
