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

## Key Resources

Read this file for context:
- [docs/ai/TROUBLESHOOTING.md](docs/ai/TROUBLESHOOTING.md) - Common issues and fixes

## Quick Diagnostics

```bash
# Check orchestrator status
issue-orchestrator status

# List tmux sessions
tmux list-windows -t orchestrator

# Check web API
curl -s http://localhost:8080/api/status | jq

# Check locks
ls -la /tmp/issue-orchestrator/locks/

# View session output
issue-orchestrator output <issue_number>
```

## Common Issues

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| Sessions fail without completion | Prompt missing `agent-done` | Add agent-done instructions to prompt |
| Pre-push hook hangs | Infinite recursion in worktree | Check hook chain, see TROUBLESHOOTING.md |
| iTerm2 very slow | Too many idle tabs | Run cleanup or restart orchestrator |
| Stale locks | Crashed session | `rm -rf /tmp/issue-orchestrator/locks/*` |

## Claude Session Logs

Find logs for debugging agent behavior:
```bash
WORKTREE="/path/to/worktree"
ESCAPED=$(echo "$WORKTREE" | sed 's|^/|-|' | tr '/' '-')
ls -la ~/.claude/projects/$ESCAPED/
```
