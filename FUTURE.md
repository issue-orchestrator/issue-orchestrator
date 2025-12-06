# Future Enhancements

Ideas and planned features for issue-orchestrator.

## CLI Parity with Web UI Context Menu

Add CLI commands that mirror the web dashboard's right-click context menu actions. This enables tmux-mode users to have the same functionality as web-mode users.

### Proposed Commands

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

- `focus` - already exists as `switch` for tmux; extend for iTerm2
- `finder` - can work standalone by inferring worktree path from naming convention
- `retry` - can work standalone via `gh issue edit` to remove labels
- `dismiss` - requires orchestrator running (modifies in-memory history)

### UX Considerations

- Add `issue-orchestrator help` command showing available actions
- Invalid command/verb should print helpful error with suggestions
- Commands should validate issue state and print clear errors:
  - "Cannot focus issue #42 - not currently running"
  - "Cannot retry issue #42 - still running"

## TUI Dashboard Enhancements

- Numbered menu for quick actions (press 1-9 to select action)
- Keybindings for common operations (r=retry, d=dismiss, f=focus)
- Status bar showing current filter/milestone

## Other Ideas

- Persistent history across sessions (SQLite or JSON file)
- Webhook support for GitHub events (instant queue updates)
- Slack/Discord notifications for session completions
- Metrics/analytics dashboard (success rate, avg runtime, etc.)
