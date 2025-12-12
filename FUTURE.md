# Future Enhancements

Ideas and planned features for issue-orchestrator.

## CLI Parity with Web Dashboard

Add CLI commands that mirror the web dashboard's context menu actions. This enables tmux-mode users and scripting to have the same functionality as web-mode users.

### Current Web Context Menu Actions

The web dashboard provides these right-click actions:
- **Focus** - Switch to the iTerm2 tab for a running session
- **Open in Finder** - Open the worktree directory
- **View Prompt** - Show the agent's prompt file
- **View Issue** - Open GitHub issue in browser
- **View PR** - Open pull request in browser (if exists)
- **Retry** - Clear labels and allow re-processing (for blocked/history items)
- **Dismiss** - Remove from history display

### Proposed CLI Commands

```bash
# For running/blocked sessions
issue-orchestrator focus <issue>    # Focus tmux/iTerm2 session
issue-orchestrator finder <issue>   # Open worktree in Finder

# For history/blocked items
issue-orchestrator retry <issue>    # Clear labels, allow re-processing
issue-orchestrator dismiss <issue>  # Remove from history display

# Queue management
issue-orchestrator prioritize <issue>  # Move to front of queue (rename 'next')
```

### Validity Matrix

| Command    | Running | Queued | Blocked/Human | History |
|------------|---------|--------|---------------|---------|
| focus      | Y       | N      | Y*            | N       |
| finder     | Y       | N      | Y (worktree)  | N       |
| retry      | N       | N      | Y             | Y       |
| dismiss    | N       | N      | N             | Y       |

*if session still exists

### Implementation Notes

- `focus` - already exists as `attach` for tmux; extend for iTerm2
- `finder` - can work standalone by inferring worktree path from naming convention
- `retry` - can work standalone via `gh issue edit` to remove labels
- `dismiss` - requires orchestrator running (modifies in-memory history)

### UX Considerations

- Invalid command/context should print helpful error with suggestions:
  - "Cannot focus issue #42 - not currently running"
  - "Cannot retry issue #42 - still running"

## Dependency-Aware Scheduling

Honor issue dependencies when scheduling. If an issue says "depends on #123" in its body, don't start it until #123 is closed.

### Current State

The `analyze_dependencies()` method exists in `scheduler.py` and can parse patterns like:
- "blocked by #123"
- "depends on #123"
- "after #123"
- "waiting for #123"
- "requires #123"

However, this method is not currently wired into the scheduling flow.

### Implementation Plan

1. **Filter blocked issues**: In `get_available_issues()`, skip issues whose blockers are still open
2. **Track blocked issues**: Return a separate list of `(issue, blocking_issues)` tuples
3. **UI visibility**: Show "Waiting on Dependencies" section in web dashboard
4. **Logging**: Print `Skipping #42 - depends on #123 (still open)` in orchestrator logs
5. **Auto-unblock**: Works naturally - next poll iteration sees blocker closed, issue becomes available

### Design Decisions

| Question | Decision |
|----------|----------|
| Blocker not in our issue set? | Make API call to check state (cache result) |
| Cross-repo dependencies? | Out of scope for v1 |
| Circular dependencies? | Detect and warn, skip both issues |
| Blocker closed but PR not merged? | Just check if issue is closed |

### UI Changes

- Add "Waiting on Dependencies" section between "Running" and "Queue"
- Show each blocked issue with its unmet dependencies as clickable links
- Badge showing count of dependency-blocked issues

## Config Sync Investigation (Archived Learning)

**Status**: Investigated and decided NOT to implement. Current approach is sufficient.

### Problem Statement

Keeping YAML config schema, CLI arguments, and config processing in sync could lead to drift. Explored options to enforce a single source of truth.

### Options Explored

1. **Validation tests**: Catch drift after the fact (reactive, not preventive)
2. **Registry-based dynamic imports**: Auto-discover config fields (not very Pythonic)
3. **Dataclass field metadata**: Attach YAML paths and CLI flags to field definitions
   ```python
   def config_meta(*, yaml_path: str, cli_flag: str | None, help: str) -> dict:
       return {"yaml_path": yaml_path, "cli_flag": cli_flag, "help": help}

   max_concurrent_sessions: int = field(
       default=3,
       metadata=config_meta(yaml_path="concurrency.max_concurrent_sessions",
                           cli_flag="--max-sessions", help="Max sessions")
   )
   ```
4. **pydantic-settings**: Off-the-shelf library for YAML + CLI + env vars

### pydantic-settings Prototype Findings

Prototyped approach #4 with `pydantic-settings`. Key findings:

- **Still requires ~90 LOC of custom glue code**:
  - Custom YAML source (~60 LOC) - pydantic-settings doesn't natively support nested YAML
  - Nested model flattening (~30 LOC) - CLI flags need flat namespace
  - CLI argument generation - argparse integration
- **Trade-offs**:
  - Gains: Type validation, env var support, cleaner validation errors
  - Costs: New dependency, custom glue code maintenance, migration effort
- **Verdict**: Not truly "off the shelf" - still requires significant custom code

### Decision

**Current dataclass-based config is sufficient.** Reasons:

1. Config drift hasn't been an actual problem (theoretical concern)
2. 90% test coverage already catches issues
3. Adding ~90 LOC to prevent a theoretical problem is yak-shaving
4. Revisit only if drift becomes a real, recurring issue

### Key Insight

> "Don't solve theoretical problems with real complexity. Current solution works."

## Other Ideas

- Persistent history across sessions (SQLite or JSON file)
- Webhook support for GitHub events (instant queue updates)
- Slack/Discord notifications for session completions
- Metrics/analytics dashboard (success rate, avg runtime, etc.)
- Keyboard shortcuts in web dashboard (r=retry, d=dismiss, f=focus)
