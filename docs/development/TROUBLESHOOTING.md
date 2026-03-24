# Troubleshooting

## Quick Debugging

**Check what's running:**
```bash
issue-orchestrator status
```

**See session output:**
```bash
issue-orchestrator output <issue_number>
```

**Attach to session:**
```bash
issue-orchestrator attach <issue_number>
```

**Check web dashboard:**
```bash
curl -s http://localhost:8080/api/status | jq
curl -s http://localhost:8080/api/state | jq
```

## Audit Surfaces

The repo has multiple things called "audit". They answer different questions.

**Queue audit:** why an issue is queued, skipped, blocked, or already in progress.
```bash
issue-orchestrator audit
curl -s "http://localhost:8080/control/tools/audit?repo_root=$(pwd)" | jq
```

**Issue audit:** force a fresh failure diagnosis for one issue or stalled run.
This is the right tool when a coding/review session timed out, never wrote
`coding-done`, or looks off relative to the timeline.
```bash
curl -s -X POST "http://localhost:8080/api/issues/4057/audit" | jq
curl -s "http://localhost:8080/api/failure-diagnosis/4057" | jq
```

**Session diagnostics:** inspect the run-scoped manifest and artifact actions for
the latest run or a specific `run_dir`.
```bash
curl -s "http://localhost:8080/api/dialog/session-diagnostics/4057" | jq
curl -s "http://localhost:8080/api/session/manifest/4057" | jq
```

Use them in this order:
1. Queue audit when the issue never started.
2. Issue audit when a specific run failed or timed out.
3. Session diagnostics when you need exact run-scoped files and replay paths.

## Session Output Directory

All session artifacts are centralized in a run directory per session:

```
<worktree>/.issue-orchestrator/sessions/
├── <run_id>__<session_name>/     # e.g., 20260120-143052Z__issue-42
│   ├── manifest.json             # Session metadata (start time, paths, outcome)
│   ├── terminal-recording.jsonl  # Terminal output (NDJSON with base64 PTY events)
│   ├── validation-record.json    # Validation pass/fail result
│   ├── validation-stdout.log     # Validation command stdout
│   ├── validation-stderr.log     # Validation command stderr
│   ├── validation-errors.txt     # Human-readable validation errors
│   ├── orchestrator-tail.log     # Filtered orchestrator log for this session
│   └── claude-session.jsonl      # Symlink to Claude session log
├── <session_name>                # Symlink to latest run for this session
├── latest.json                   # Pointer to most recent run
└── index.json                    # List of all runs
```

**Quick navigation:**
```bash
WORKTREE="/path/to/worktree"

# Find the latest run
RUN_DIR=$(ls -td $WORKTREE/.issue-orchestrator/sessions/*__* 2>/dev/null | head -1)

# Check manifest for session metadata
cat $RUN_DIR/manifest.json | jq

# Check terminal recording (NDJSON format — use orchestrator replay, not cat)
ls -lh $RUN_DIR/terminal-recording.jsonl

# Check validation errors
cat $RUN_DIR/validation-errors.txt

# List all runs in a worktree
cat $WORKTREE/.issue-orchestrator/sessions/index.json | jq '.runs'
```

## Common Issues

### Dependency Changes Not Reflected Locally

**Symptom:** You updated `pyproject.toml`, but dependencies or `uv.lock` are out of sync.

**Fix:** Run `make upgrade-deps` to re-resolve and sync, then commit `uv.lock` alongside
the `pyproject.toml` change.

### Sessions Failing Without Completion

**Symptom:** Sessions end with "without completion markers", marked as FAILED.

**Causes:**
1. Agent prompt doesn't include `coding-done`/`reviewer-done` instructions
2. Pre-push hook blocking push
3. Agent crashing/timeout before completion

**Fix:** Ensure agent prompts include `coding-done`/`reviewer-done` usage in "When Done" section.

### Pre-Push Validation Failed

**Symptom:** `git push` fails with validation errors.

**Finding the output:** When validation fails, the full output is saved to a known location. The exact location depends on how validation was run:

1. **Orchestrator-managed sessions**: Output goes to the session directory
   ```
   <worktree>/.issue-orchestrator/sessions/<run_id>__<session>/validation-output.log
   ```

2. **Direct runs** (human running `make validate`): Falls back to diagnostics
   ```
   <worktree>/.issue-orchestrator/diagnostics/validation-output.log
   ```

The failure message always prints the path to the output file:
```
============================================================
Validation FAILED (exit code 1) in 45.2s
============================================================

Full output saved to:
  /path/to/worktree/.issue-orchestrator/diagnostics/validation-output.log

To view: cat /path/to/worktree/.issue-orchestrator/diagnostics/validation-output.log
============================================================
```

**How it works:** The `make validate` target runs validation through a Python wrapper (`validate_runner.py`) that captures all output while also streaming it to the terminal. This ensures agents can find failure details without re-running tests.

**Fallback:** If the Python wrapper fails, use `make validate-raw` for direct execution (no output capture).

**Environment variable:** The orchestrator sets `ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR` to direct output to the session directory. For direct runs, this is unset and output goes to the diagnostics fallback.

### Pre-Push Hook Infinite Recursion

**Symptom:** Push hangs forever, hook log shows repeated "Pre-push hook started".

**Cause:** When worktrees reused, `install_hooks()` reads `core.hooksPath` from worktree config (which has our override), copies the chained wrapper as "project hook".

**Fix:** Code now reads `core.hooksPath` from main repo only. To repair existing worktrees:
```bash
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

### Missing Labels

**Symptom:** Warnings about labels not found.

**Fix:**
```bash
gh label create "failed" -R owner/repo --description "Agent session failed" --color "B60205"
```

### Lock Cleanup

Locks stored in `.issue-orchestrator/locks/` (per-instance JSON files). Cleanup runs at startup.

Manual cleanup:
```bash
rm .issue-orchestrator/locks/*.json
```

## Claude Session Logs

Each Claude Code session creates logs useful for debugging:

**Log Locations:**
```
~/.claude/
├── projects/<escaped-path>/     # Per-project session history
│   └── <session-id>.jsonl       # Conversation history
├── debug/<session-id>.txt       # Debug logs
├── history.jsonl                # Global command history
└── todos/<session-id>-*.json    # Todo lists per session
```

**Path Escaping:** `/Users/bruce/dev/myproject` -> `-Users-bruce-dev-myproject`

**Quick access via run directory:**
```bash
# The run directory has a symlink to the Claude log
ls -la $RUN_DIR/claude-session.jsonl

# Or get the path from manifest
cat $RUN_DIR/manifest.json | jq -r '.claude_log_path'
```

**Legacy method (finding sessions for a worktree):**
```bash
WORKTREE="/path/to/worktree"
ESCAPED=$(echo "$WORKTREE" | sed 's|^/|-|' | tr '/' '-')
ls -la ~/.claude/projects/$ESCAPED/

# View most recent session log
ls -t ~/.claude/projects/$ESCAPED/*.jsonl | head -1 | xargs head -100
```
