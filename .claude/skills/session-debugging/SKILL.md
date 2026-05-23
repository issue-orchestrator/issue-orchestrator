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

## Session Output Directory Structure

All session artifacts are now centralized in a single run directory per session:

```
<worktree>/.issue-orchestrator/sessions/
├── <run_id>__<session_name>/     # e.g., 20260120-143052Z__issue-42
│   ├── manifest.json             # Session metadata (start time, paths, outcome)
│   ├── terminal-recording.jsonl  # Terminal output (NDJSON with base64 PTY events)
│   ├── validation-record.json    # Validation pass/fail result
│   ├── validation-stdout.log     # Validation command stdout
│   ├── validation-stderr.log     # Validation command stderr
│   ├── validation-errors.txt     # Human-readable validation errors
│   ├── validation-state.json     # Retry flow state (retry_count, etc.)
│   ├── retry-prompt.md           # Prompt for retry session
│   ├── failure-diagnostic-*.json # Failure diagnostics
│   ├── worktree.json             # Worktree metadata
│   ├── session-identity.json     # Stable identity for this run
│   ├── session-prompt.txt        # Prompt used for the session
│   ├── orchestrator-tail.log     # Filtered orchestrator log for this session
│   ├── claude-session.path       # Path to Claude log
│   ├── claude-session.jsonl      # Symlink to Claude session log
│   └── review-exchange/          # Local-loop round logs and transcript when used
├── <session_name>                # Symlink to latest run for this session
├── latest.json                   # Pointer to most recent run
└── index.json                    # List of all runs
```

**Quick navigation:**
```bash
# Go to the latest run for a session
cd $WORKTREE/.issue-orchestrator/sessions/<session_name>

# Or use find_run_dir pattern
ls -la $WORKTREE/.issue-orchestrator/sessions/*__issue-<N>
```

---

## Step 1: Get the Trace

**Always start here.** Run the trace command for the issue:

```bash
issue-orchestrator trace <ISSUE_NUMBER>
```

Example output:
```
2026-01-12 11:44:30 [SESSION] status=FAILED outcome=none reason=no_completion_record
2026-01-12 11:44:30 [ERROR] errors=["Push failed: terminal-recording.jsonl is 156MB"]
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
| `no_completion_record` | Agent never called `coding-done`/`reviewer-done` | Step 3A |
| `validation_failed` | Tests/lint/type-check failed | Step 3B |
| `push_failed` | Git push failed | Step 3C |
| `timeout` | Session exceeded time limit | Step 3D |
| `blocked` | Agent reported blocked | Step 3E |

---

## Review Exchange `*_no_completion` After Valid JSON

When a review exchange reports `reviewer_no_completion` or
`coder_no_completion`, first check whether that same turn produced a valid
response artifact before the process exited. A valid artifact means the turn
itself completed; a later prompt failing because the old role process is gone
is a persistent-session lifecycle issue. The expected behavior is to respawn
that role for the later turn using the same worktree and pair-scoped artifact
paths, not to reclassify the completed prior turn as failed.

This is especially common when `review.nits.*` resolves to `address`: the
reviewer can approve with nits, the orchestrator routes those nits through the
coder rework loop, and a one-shot reviewer process may need to be respawned for
the follow-up review.

---

## Step 3A: No Completion Record

The agent session terminated without calling `coding-done`/`reviewer-done`. Common causes:
- Agent died/crashed early
- Agent forgot to call `coding-done`/`reviewer-done`
- Agent was rate-limited
- Permission prompt blocked agent

### Check the Session Run Directory

```bash
WORKTREE="/path/to/worktree"  # e.g., /Users/brucegordon/dev/repo-42

# Find the latest run directory
RUN_DIR=$(ls -td $WORKTREE/.issue-orchestrator/sessions/*__issue-* 2>/dev/null | head -1)

# Check what's in it
ls -la $RUN_DIR

# View the manifest for session metadata
cat $RUN_DIR/manifest.json | jq

# Get the exact completion record path, if the agent wrote one
cat $RUN_DIR/manifest.json | jq -r '.completion_path // .completion_record_path // empty'
```

### Check Claude Session Logs

The manifest contains the Claude log path, or use the attached log:

```bash
# From manifest
cat $RUN_DIR/manifest.json | jq -r '.claude_log_path'

# Or check the attached symlink
ls -la $RUN_DIR/claude-session.jsonl

# Check session length
wc -l $RUN_DIR/claude-session.jsonl
```

**Interpret the session length:**
- **< 20 lines**: Session died almost immediately (crash, rate limit, error)
- **20-100 lines**: Session started but failed early
- **> 100 lines**: Session ran but didn't complete properly

### Check for completion command calls

```bash
# Did the agent ever call coding-done or reviewer-done?
grep -E "coding-done|reviewer-done" $RUN_DIR/claude-session.jsonl

# If found, check the result
grep -E -A5 "coding-done|reviewer-done" $RUN_DIR/claude-session.jsonl | grep "tool_result"
```

### Check for errors in session

```bash
# Look for errors
grep -i "error" $RUN_DIR/claude-session.jsonl | head -10

# Look for rate limiting
grep -i "limit\|rate\|quota" $RUN_DIR/claude-session.jsonl
```

**Common findings:**
- No completion command + short session = Agent crashed or was killed
- `coding-done`/`reviewer-done` called but validation failed = Go to Step 3B
- Permission errors = Agent hit permission prompt (needs `bypassPermissions`)

---

## Step 3B: Validation Failed

The agent called `coding-done completed` but validation (tests/lint) failed.

### Check validation output

```bash
# View validation record
cat $RUN_DIR/validation-record.json | jq

# View human-readable errors
cat $RUN_DIR/validation-errors.txt

# View full stdout/stderr
cat $RUN_DIR/validation-stdout.log
cat $RUN_DIR/validation-stderr.log
```

### Check retry state

```bash
# See if retries were attempted
cat $RUN_DIR/validation-state.json | jq

# View retry prompt if it exists
cat $RUN_DIR/retry-prompt.md
```

### Check what failed

Look for in the trace or validation output:
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
| `file exceeds 100MB` | Large file (often terminal-recording.jsonl) | Add to .gitignore |
| `stale info` | Branch diverged from remote | Rebase needed |
| `permission denied` | Auth issue | Check git credentials |
| `protected branch` | Can't push to main | Should be on feature branch |

### Check for large files

```bash
# Find large files in worktree
find $WORKTREE -type f -size +10M -exec ls -lh {} \;

# Check terminal recording size
ls -lh $RUN_DIR/terminal-recording.jsonl

# Check .gitignore
cat $WORKTREE/.gitignore | grep -E "terminal-recording|\.jsonl|\.log"
```

---

## Step 3D: Timeout

Session exceeded the configured time limit.

### Check session duration

```bash
# View timing from manifest
cat $RUN_DIR/manifest.json | jq '{started_at, ended_at}'

# From trace output, look for timing
grep -E "runtime|duration|timeout" in trace output
```

**Resolution:**
- Increase timeout in config if task genuinely needs more time
- Check if agent was stuck in a loop
- Check Claude session log for what agent was doing when killed

---

## Step 3E: Agent Reported Blocked

The agent successfully called `coding-done blocked`.

### Check why agent blocked

```bash
# View manifest for outcome and reason
cat $RUN_DIR/manifest.json | jq '{outcome, blocked_reason}'

# Check completion file recorded in the manifest
COMPLETION=$(cat $RUN_DIR/manifest.json | jq -r '.completion_path // .completion_record_path // empty')
test -n "$COMPLETION" && cat "$WORKTREE/$COMPLETION" | jq '.blocked_reason'
```

**This is expected behavior** - the agent determined it couldn't proceed and reported properly. Review the blocked reason to understand why.

---

## Quick Reference

### Session Run Directory Files

| File | Purpose |
|------|---------|
| `manifest.json` | Session metadata: times, paths, outcome, validation status |
| `terminal-recording.jsonl` | Terminal output (NDJSON with base64 PTY events) |
| `validation-record.json` | Structured validation result (passed, exit_code, command) |
| `validation-stdout.log` | Raw stdout from validation command |
| `validation-stderr.log` | Raw stderr from validation command |
| `validation-errors.txt` | Human-readable error summary |
| `validation-state.json` | Retry flow state (retry_count, max_retries) |
| `session-prompt.txt` | Prompt used to launch the session |
| `session-identity.json` | Run/session identity metadata |
| `orchestrator-tail.log` | Filtered orchestrator log for this session |
| `claude-session.jsonl` | Symlink to Claude session log |
| `review-exchange/` | Local review loop round recordings and transcript |
| `failure-diagnostic-*.json` | Detailed failure analysis |

### Key Commands

```bash
# Trace an issue
issue-orchestrator trace <ISSUE_NUMBER>

# Check orchestrator status
issue-orchestrator status

# View session output
issue-orchestrator output <ISSUE_NUMBER>

# Find latest run directory for an issue
ls -td $WORKTREE/.issue-orchestrator/sessions/*__issue-<N> | head -1

# List all runs in a worktree
cat $WORKTREE/.issue-orchestrator/sessions/index.json | jq '.runs'

# Get latest run info
cat $WORKTREE/.issue-orchestrator/sessions/latest.json | jq
```

---

## Escalation

If you can't determine the cause from the above:

1. Check the session-specific orchestrator tail:
   ```bash
   cat $RUN_DIR/orchestrator-tail.log | tail -100
   ```

2. Check the full orchestrator log:
   ```bash
   grep "\[issue-<N>\]" .issue-orchestrator/state/logs/orchestrator.log | tail -100
   ```

3. Look for [FAILURE_CONTEXT] automatic analysis:
   ```bash
   grep "\[FAILURE_CONTEXT\]" .issue-orchestrator/state/logs/orchestrator.log | tail -20
   ```

4. Check for infrastructure issues (use `troubleshooting` skill for hooks, locks, performance)
