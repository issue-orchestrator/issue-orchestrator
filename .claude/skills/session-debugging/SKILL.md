---
name: session-debugging
description: Investigate why an issue or session failed. Use when an issue shows "blocked", a session didn't complete, or you need to understand why agent work failed. Start here for "why did issue X fail" questions.
---

# Session Debugging

Prescriptive workflow for investigating failed issues/sessions.

## When to Use

- "Why did issue #X fail?"
- "Issue shows blocked-failed"
- "Session didn't create a PR"
- "Agent work was lost"
- Investigating `no_completion_record` failures

---

## Step 1: Get the Trace

**Always start here.** Run the trace command for the issue:

```bash
issue-orchestrator trace <ISSUE_NUMBER>
```

Example output:
```
2026-01-12 11:44:30 [SESSION] status=FAILED outcome=none reason=no_completion_record
2026-01-12 11:44:30 [ERROR] errors=["Push failed: session.log is 156MB"]
```

**What to look for:**
- `reason=` - The failure classification
- `errors=` - Specific error messages
- `outcome=` - What the agent reported (if anything)

---

## Step 2: Identify Failure Reason

Based on the `reason=` value, go to the appropriate section:

| Reason | Meaning | Go to |
|--------|---------|-------|
| `no_completion_record` | Agent never called `agent-done` | Step 3A |
| `validation_failed` | Tests/lint/type-check failed | Step 3B |
| `push_failed` | Git push failed | Step 3C |
| `timeout` | Session exceeded time limit | Step 3D |
| `blocked` | Agent reported blocked | Step 3E |

---

## Step 3A: No Completion Record

The agent session terminated without calling `agent-done`. Common causes:
- Agent died/crashed early
- Agent forgot to call agent-done
- Agent was rate-limited
- Permission prompt blocked agent

### Check Claude Session Logs

```bash
# Find the worktree
WORKTREE="/Users/brucegordon/dev/issue-orchestrator-<ISSUE_NUMBER>"

# Convert to Claude project path
ESCAPED=$(echo "$WORKTREE" | sed 's|^/|-|' | tr '/' '-')

# List sessions (most recent first)
ls -lt ~/.claude/projects/$ESCAPED/*.jsonl | head -3

# Check session length
wc -l ~/.claude/projects/$ESCAPED/<latest-session>.jsonl
```

**Interpret the session length:**
- **< 20 lines**: Session died almost immediately (crash, rate limit, error)
- **20-100 lines**: Session started but failed early
- **> 100 lines**: Session ran but didn't complete properly

### Check for agent-done calls

```bash
# Did the agent ever call agent-done?
grep "agent-done" ~/.claude/projects/$ESCAPED/<session>.jsonl

# If found, check the result
grep -A5 "agent-done" ~/.claude/projects/$ESCAPED/<session>.jsonl | grep "tool_result"
```

### Check for errors in session

```bash
# Look for errors
grep -i "error" ~/.claude/projects/$ESCAPED/<session>.jsonl | head -10

# Look for rate limiting
grep -i "limit\|rate\|quota" ~/.claude/projects/$ESCAPED/<session>.jsonl
```

**Common findings:**
- No agent-done + short session = Agent crashed or was killed
- agent-done called but validation failed = Go to Step 3B
- Permission errors = Agent hit permission prompt (needs `bypassPermissions`)

---

## Step 3B: Validation Failed

The agent called `agent-done completed` but validation (tests/lint) failed.

### Check validation output

```bash
# Find validation records
ls -la $WORKTREE/.issue-orchestrator/validation/

# View latest validation result
cat $WORKTREE/.issue-orchestrator/validation/*.json | jq
```

### Check what failed

Look for in the trace:
```
STDERR: FAILED tests/unit/test_foo.py::test_something
```

**Resolution:** The agent should have fixed the failing tests. Check if:
1. Agent saw the failure but gave up
2. Agent tried to fix but made it worse
3. Tests were flaky/environmental

---

## Step 3C: Push Failed

Git push to remote failed.

### Common push failures

| Error | Cause | Fix |
|-------|-------|-----|
| `file exceeds 100MB` | Large file (often session.log) | Add to .gitignore |
| `stale info` | Branch diverged from remote | Rebase needed |
| `permission denied` | Auth issue | Check git credentials |
| `protected branch` | Can't push to main | Should be on feature branch |

### Check for large files

```bash
# Find large files in worktree
find $WORKTREE -type f -size +10M -exec ls -lh {} \;

# Check .gitignore
cat $WORKTREE/.gitignore | grep -E "session\.log|\.log"
```

---

## Step 3D: Timeout

Session exceeded the configured time limit.

### Check session duration

```bash
# From trace output, look for timing
grep -E "runtime|duration|timeout" in trace output
```

**Resolution:**
- Increase timeout in config if task genuinely needs more time
- Check if agent was stuck in a loop
- Check Claude session log for what agent was doing when killed

---

## Step 3E: Agent Reported Blocked

The agent successfully called `agent-done blocked`.

### Check why agent blocked

```bash
# View completion record
cat $WORKTREE/.issue-orchestrator/completion*.json | jq '.blocked_reason'
```

**This is expected behavior** - the agent determined it couldn't proceed and reported properly. Review the blocked reason to understand why.

---

## Quick Reference

### Key Files

| Location | Purpose |
|----------|---------|
| `.issue-orchestrator/completion*.json` | Agent's completion record |
| `.issue-orchestrator/validation/*.json` | Validation results |
| `.issue-orchestrator/session.log` | Session output (may be large) |
| `~/.claude/projects/<escaped-path>/*.jsonl` | Claude session logs |

### Key Commands

```bash
# Trace an issue
issue-orchestrator trace <ISSUE_NUMBER>

# Check orchestrator status
issue-orchestrator status

# View session output
issue-orchestrator output <ISSUE_NUMBER>

# Check if completion file exists
ls -la $WORKTREE/.issue-orchestrator/completion*.json
```

---

## Escalation

If you can't determine the cause from the above:

1. Check the full orchestrator log:
   ```bash
   grep "\[issue-<N>\]" .issue-orchestrator/state/logs/orchestrator.log | tail -100
   ```

2. Look for [FAILURE_CONTEXT] automatic analysis:
   ```bash
   grep "\[FAILURE_CONTEXT\]" .issue-orchestrator/state/logs/orchestrator.log | tail -20
   ```

3. Check for infrastructure issues (use `troubleshooting` skill for hooks, locks, performance)
