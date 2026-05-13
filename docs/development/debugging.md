# Debugging the Issue Orchestrator

This page is the quick reference for tracing the current runtime. For issue-level failure analysis, prefer the `session-debugging` skill or `issue-orchestrator trace`.

## Event Flow

Events still flow through the `EventSink` and pluggy-backed adapters:

```text
TraceEvent(EventName.X, data)
  -> PluggyEventSink.publish()
  -> registered plugins
     -> lifecycle logging
     -> SSE broadcast
     -> any test hooks
```

Key files:
- `src/issue_orchestrator/events/catalog.py`
- `src/issue_orchestrator/ports/event_sink.py`
- `src/issue_orchestrator/execution/event_sink_adapter.py`
- `src/issue_orchestrator/execution/lifecycle_logging.py`
- `src/issue_orchestrator/entrypoints/bootstrap.py`

Use `EventName` constants, never raw strings:

```python
from issue_orchestrator.events import EventName
from issue_orchestrator.ports import TraceEvent

self.events.publish(TraceEvent(EventName.SESSION_STARTED, {"issue_number": 123}))
```

## First Places To Look

### 1. Trace output

```bash
issue-orchestrator trace <ISSUE_NUMBER>
```

Start here when one issue failed or stalled. It gives you the classified reason (`no_completion_record`, `validation_failed`, `push_failed`, `timeout`, `blocked`) before you start digging through files.

### 1b. Force an issue audit

```bash
curl -s -X POST "http://localhost:8080/api/issues/<ISSUE_NUMBER>/audit" | jq
```

Use this when you want a fresh issue/session failure diagnosis now. This is not
the same as queue audit:

- `issue-orchestrator audit` answers why issues are queued or skipped.
- `POST /api/issues/<ISSUE_NUMBER>/audit` answers what went wrong in the
  current or latest run for one issue.

### 2. Orchestrator log

```bash
LOG=".issue-orchestrator/state/logs/orchestrator.log"
tail -f "$LOG"
grep "\[EVENT\]" "$LOG" | tail -100
grep -E "\[EVENT\]|\[STATE_MACHINE\]|Launched|Queued|review" "$LOG" | tail -100
```

The runtime log now lives under repo state, not a fixed home-directory path.

To include DEBUG diagnostics in that log, start the engine with debug logging:

```bash
issue-orchestrator start --debug
```

For Repository Engines launched by Control Center, start Control Center with
`--debug`, or set `ISSUE_ORCHESTRATOR_ENGINE_LOG_LEVEL=DEBUG` before starting
Control Center. The supervisor passes that value through to the engine process.

### 3. Session run directories

```bash
WORKTREE="/path/to/worktree"
ls -la "$WORKTREE/.issue-orchestrator/sessions"
RUN_DIR=$(ls -td "$WORKTREE/.issue-orchestrator/sessions/"*__* | head -1)
cat "$RUN_DIR/manifest.json" | jq
tail -100 "$RUN_DIR/session.log"
```

Current sessions are recorded in per-run directories such as:

```text
<worktree>/.issue-orchestrator/sessions/<run_id>__<session_name>/
```

Useful files in a run directory:
- `manifest.json`
- `session.log`
- `orchestrator-tail.log`
- `claude-session.jsonl`
- `validation-record.json`
- `validation-stdout.log`
- `validation-stderr.log`

## Flow Checklist

When tracing a normal issue -> PR -> review cycle, verify these stages:

1. Issue is eligible and claimed.
2. Session launches and writes a run directory.
3. Agent calls `coding-done`.
4. Validation artifacts are written.
5. Orchestrator processes completion and creates or updates the PR.
6. Review session launches.
7. Reviewer calls `reviewer-done`.
8. Final labels and review state match the verdict.

## Useful Commands

```bash
# Start the local control center helper
scripts/start_control_center.sh

# Follow orchestrator events
LOG=".issue-orchestrator/state/logs/orchestrator.log"
grep "\[EVENT\]" "$LOG" | tail -100

# Inspect the latest run for an issue session
WORKTREE="/path/to/worktree"
ls -td "$WORKTREE/.issue-orchestrator/sessions/"*__issue-* | head -1

# Find validation failures in the latest run
RUN_DIR=$(ls -td "$WORKTREE/.issue-orchestrator/sessions/"*__* | head -1)
cat "$RUN_DIR/validation-errors.txt"

# Run one e2e test locally
make test-e2e-one TEST=test_code_review_produces_review_comment
```

## Common Failure Angles

- Review never starts: verify review config, labels, and review queue events.
- Session exits without completion: inspect `claude-session.jsonl` for missing `coding-done` / `reviewer-done`.
- Validation failed: inspect `validation-record.json` and the stderr/stdout logs in the run directory.
- Labels look wrong: inspect the completion record and downstream action application, not just the UI.
