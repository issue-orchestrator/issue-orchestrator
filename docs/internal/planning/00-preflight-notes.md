# Preflight Notes

## Current Module Paths

### Worktree Creation
- **Primary**: `src/issue_orchestrator/worktree.py`
  - `create_worktree()` - main entry point (line 315)
  - `install_hooks()` - installs git hooks (line 100)
  - `install_claude_settings()` - installs Claude Code settings (line 41)
- **Called from**: `orchestrator.py:888`, `orchestrator.py:1583`, `orchestrator.py:2254`

### Agent Session Launching
- **Session manager**: `src/issue_orchestrator/control/session_manager.py`
  - `SessionManager.start()` - launches sessions via runner port
- **Orchestrator**: `src/issue_orchestrator/orchestrator.py`
  - `launch_session()` - high-level launch logic
  - `_create_session()` - actual session creation
- **Terminal adapters**: `src/issue_orchestrator/execution/`
  - `terminal_tmux.py` - tmux sessions
  - `terminal_iterm.py` - iTerm2 sessions

### agent_done Implementation
- **Primary**: `src/issue_orchestrator/agent_done.py`
  - Writes completion record to `.issue-orchestrator/completion.json`
  - Does NOT push, create PRs, or modify labels
  - Agent reports intent; orchestrator executes
- **Completion processing**: `src/issue_orchestrator/control/completion_processor.py`
  - `CompletionProcessor.process()` - reads and validates completion records
  - `execute_requested_actions()` - executes push/PR/comment/labels

### Publish Actions (push/PR/comment/labels)
- **Completion processor**: `src/issue_orchestrator/control/completion_processor.py`
  - `execute_requested_actions()` - handles PUSH_BRANCH, CREATE_PR, POST_COMMENT, labels
- **GitHub adapter**: `src/issue_orchestrator/execution/github_adapter.py`
  - `create_pr()`, `add_comment()`, `add_label()`, `remove_label()`
- **Label sync**: `src/issue_orchestrator/control/label_sync.py`
  - Idempotent label synchronization

### Existing Validation/Hook Logic
- **AI meta-agent hooks**: `src/issue_orchestrator/hooks.py`
  - `ClaudeCodeAdapter.install_hooks()` - installs PreToolUse hooks
  - `ClaudeCodeAdapter.verify_hooks()` - verifies hooks work
  - Blocks `--no-verify` and `gh pr merge`
- **Git hooks**: `src/issue_orchestrator/templates/hooks/git/`
  - `pre-push-wrapper.sh` - chains project + orchestrator hooks
  - `pre-push-orchestrator.sh` - validates Agent-Status trailers
- **Hook installation**: `src/issue_orchestrator/worktree.py:install_hooks()`

### Config
- **Primary**: `src/issue_orchestrator/config.py`
  - `Config` dataclass - all configuration options
  - Currently no validation-related config keys

## Key Observations

1. **No validation gate currently exists** - agents can complete without running tests
2. **No env scrubbing** - agent sessions inherit parent environment
3. **No sandbox verification** - agents could have access to credentials
4. **publish actions are in completion_processor** - this is where publish gate should be added

## Implementation Targets

1. Add to `Config`:
   - `validation.publish_gate.cmd`
   - `validation.agent_gate.cmd`
   - `validation_policy`
   - `isolation.mode`

2. Create new modules:
   - `src/issue_orchestrator/control/validation.py` - runner + cache
   - `src/issue_orchestrator/control/sandbox.py` - env scrubbing + verification

3. Modify:
   - `agent_done.py` - optional agent_gate validation
   - `completion_processor.py` - publish gate before push/PR
   - `worktree.py` or `session_manager.py` - sandbox verification + env scrubbing
