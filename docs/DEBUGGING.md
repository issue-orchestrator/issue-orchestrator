# Debugging the Issue Orchestrator

## Event System Architecture (DON'T FORGET THIS)

Events flow through pluggy hooks:
```
TraceEvent(name, data) --> PluggyEventSink --> on_trace_event hook
                                                    |
                    +-------------------------------+-------------------------------+
                    |                               |                               |
             LifecycleIPCPlugin           LifecycleSSEPlugin           LifecycleLoggingPlugin
             (UNUSED - unit tests only)            |                               |
                                                   v                               v
                                           Web UI /api/events              Python logger
                                                                     (~/.issue-orchestrator.log)
```

**IMPORTANT**: LifecycleIPCPlugin is registered but nothing connects to it.
The web UI uses SSE (LifecycleSSEPlugin), not IPC.

**Key files:**
- `bootstrap.py` - Registers LifecycleLoggingPlugin (line ~100)
- `execution/lifecycle_logging.py` - Logs events to `issue_orchestrator.events` logger
- `execution/event_sink_adapter.py` - PluggyEventSink.publish() broadcasts events
- `ports/event_sink.py` - TraceEvent dataclass

**How to emit events:**
```python
self.events.publish(TraceEvent("session.started", {"issue_number": 123}))
```

**How events appear in logs:**
```
[EVENT] session.started: issue_number=123, session_id=issue-123
```

**Enable stderr logging for e2e tests:**
```bash
export ORCHESTRATOR_LOG_TO_STDERR=1
```
This is already set in `conftest.py` line 522.

## Quick Reference: Where to Look

### 1. Orchestrator Log
```bash
tail -f ~/.issue-orchestrator.log
```
- Shows loop iterations, session starts/stops
- **Problem**: Currently only shows `[LOOP] Iteration N - active=X, pending_reviews=Y`
- **Need**: More detailed event logging

### 2. Session Worktrees
```bash
ls -la /tmp/e2e-worktrees/
```
- Named `issue-orchestrator-{issue_number}` for issue sessions
- Named `review-{pr_number}` for review sessions (TODO: verify naming)

### 3. Completion Records
```bash
cat /tmp/e2e-worktrees/issue-orchestrator-{N}/.issue-orchestrator/completion.json
```
- Shows outcome: `completed`, `review_approved`, `changes_requested`, etc.
- Shows `requested_actions`: labels to add/remove, comments to post

### 4. Session Logs (Claude CLI output)
```bash
cat /tmp/e2e-worktrees/issue-orchestrator-{N}/.issue-orchestrator/session.log
```
- Raw terminal output from Claude session
- Shows what commands were run
- Shows any errors

## Understanding the Flow

### Issue → PR → Review Flow
1. Issue created with `agent:X` label
2. Orchestrator creates worktree, launches Claude with agent prompt
3. Agent runs `agent-done completed` → creates completion.json
4. Orchestrator processes completion: pushes branch, creates PR
5. PR gets `needs-code-review` label
6. Orchestrator launches review session with `code_review_agent`
7. Review agent runs `agent-done approved` or `agent-done changes-requested`
8. Orchestrator processes: adds `code-reviewed` or `needs-rework`, removes `needs-code-review`

### Key Config Settings
```yaml
# .issue-orchestrator.yaml
agents:
  "agent:e2e-test":
    prompt: "examples/prompts/e2e-test.md"  # Issue work prompt
  "agent:e2e-test-approves":
    prompt: "examples/prompts/e2e-test-approves.md"  # Review prompt

review:
  code_review_agent: "agent:e2e-test-approves"  # Which agent does reviews
  code_review_label: "needs-code-review"
  code_reviewed_label: "code-reviewed"
```

## Common Issues

### Issue: Review not running
**Symptoms**: PR has `needs-code-review` but never gets `code-reviewed`
**Check**:
1. Is `code_review_agent` configured in yaml?
2. Is the review agent defined in `agents:` section?
3. Check orchestrator log for review session start

### Issue: Wrong labels applied
**Symptoms**: Labels applied to wrong issue/PR
**Check**:
1. Look at completion.json - what's the `label_target`?
2. For reviews, labels should target PR number, not issue number

### Issue: Session stuck
**Symptoms**: Session doesn't complete
**Check**:
1. `tmux list-sessions` - is session still running?
2. Check session.log for errors
3. Check if agent-done was called

## Adding More Logging

### Current Gaps
- Orchestrator loop doesn't log session lifecycle events
- No visibility into what GitHub API calls are made
- No easy way to trace issue → session → completion flow

### Proposed Improvements
1. Add event emission for all session lifecycle events
2. Log label operations with issue/PR numbers
3. Create web UI endpoint for viewing event log
4. Add timestamps to all events

## Debugging Commands

```bash
# Find recent completions
find /tmp/e2e-worktrees -name "completion.json" -mmin -60

# Check GitHub for PR labels
gh pr view {PR_NUMBER} --json labels

# Check issue state
gh issue view {ISSUE_NUMBER} --json labels,state

# See ONLY events (filter out loop spam)
grep "\[EVENT\]" ~/.issue-orchestrator.log | tail -100

# See events + key transitions
grep -E "\[EVENT\]|\[STATE_MACHINE\]|Launched|Queued|review" ~/.issue-orchestrator.log | tail -100

# Find what orchestrator is doing (exclude loop iterations)
grep -v "LOOP.*Iteration" ~/.issue-orchestrator.log | tail -100

# Kill stuck orchestrators
pkill -f "issue-orchestrator.*start"

# Run single e2e test
make test-e2e-one TEST=test_code_review_produces_review_comment
```

## Events That Should Be Emitted

Check these events are being logged during a test run:

| Event | When | Key Data |
|-------|------|----------|
| `session.started` | Issue session launched | issue_number, session_id |
| `session.completed` | Session finished | issue_number, outcome, actions_taken |
| `pr.created` | PR created | issue_number, pr_url |
| `review.queued` | PR queued for review | pr_number, issue_number |
| `review.started` | Review session launched | pr_number |
| `review.approved` | Review approved | pr_number |
| `review.changes_requested` | Review rejected | pr_number |

If you don't see these events, the orchestrator isn't reaching those code paths.
