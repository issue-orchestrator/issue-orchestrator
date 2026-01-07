# Adapters

**Purpose**: Implementations of external system integrations (GitHub API, tmux/iTerm2, git worktrees).

**Boundaries**:
- Adapters implement port interfaces from `ports/`
- No business logic here - only translation between domain types and external APIs
- Each subdirectory isolates one external system
- Internal implementation details (prefixed with `_`) are not exported
