# Troubleshooting

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
tmux attach -t orchestrator:issue-<number>
```

**Check web dashboard:**
```bash
curl -s http://localhost:8080/api/status | jq
curl -s http://localhost:8080/api/state | jq
```

## Common Issues

### Sessions Failing Without Completion

**Symptom:** Sessions end with "without completion markers", marked as FAILED.

**Causes:**
1. Agent prompt doesn't include `agent-done` instructions
2. Pre-push hook blocking push
3. Agent crashing/timeout before completion

**Fix:** Ensure agent prompts include `agent-done` usage in "When Done" section.

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

### iTerm2 Slowdown

**Symptom:** Creating new tabs takes 30-60+ seconds.

**Cause:** Too many accumulated idle tabs.

**Fix:**
```bash
python -c "from issue_orchestrator.iterm2 import cleanup_idle_tabs; cleanup_idle_tabs()"
```
Or restart orchestrator (cleanup runs at startup for iTerm2/web modes).

### Missing Labels

**Symptom:** Warnings about labels not found.

**Fix:**
```bash
gh label create "failed" -R owner/repo --description "Agent session failed" --color "B60205"
```

### Lock Cleanup

Locks stored in `/tmp/issue-orchestrator/locks/`. Cleanup runs at startup.

Manual cleanup:
```bash
rm -rf /tmp/issue-orchestrator/locks/*
```

## Test Failures in Worktree Environment

**Symptom:** Tests pass when run from the main repo, but fail when run from an orchestrator-managed worktree. Error: `make: *** No rule to make target 'validate'.  Stop.`

**Cause:** Environment variable leakage. The orchestrator sets `ORCHESTRATOR_VALIDATION_CMD=make validate` for agent sessions. When pytest runs in that environment, tests that create isolated tmp_path directories still inherit these env vars:

```
Orchestrator (main repo)
    │ sets ORCHESTRATOR_VALIDATION_CMD=make validate
    ▼
Worktree (agent works here)
    │ pytest runs, inherits env vars
    ▼
Test (tmp_path)
    │ creates config with custom validation cmd
    │ BUT: env var overrides config!
    ▼
agent-done runs `make validate` instead of config's cmd
    │ No Makefile in tmp_path → CRASH
```

**Fix:** Tests that verify config-based validation must clear orchestrator env vars:

```python
def test_validation_xxx(self, tmp_path, monkeypatch):
    # Clear orchestrator env vars so test uses config's validation cmd
    monkeypatch.delenv("ORCHESTRATOR_VALIDATION_CMD", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_VALIDATION_TIMEOUT", raising=False)
    # ... rest of test
```

**Related:** See `tests/unit/test_agent_done.py::TestAgentGateIntegration` docstring for full details.

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

**Find sessions for a worktree:**
```bash
WORKTREE="/path/to/worktree"
ESCAPED=$(echo "$WORKTREE" | sed 's|^/|-|' | tr '/' '-')
ls -la ~/.claude/projects/$ESCAPED/

# View most recent session log
ls -t ~/.claude/projects/$ESCAPED/*.jsonl | head -1 | xargs head -100
```
