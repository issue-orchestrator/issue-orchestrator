# ADR 0018: Git worktree isolation per agent session

**Status:** Accepted
**Date:** 2024-12-21

## Context

Multiple agents work on different issues concurrently. They need:
- Isolated file systems (can't step on each other's changes)
- Independent git state (different branches, commits)
- Separate environments (no credential leakage between sessions)

Options considered:
1. **Shared repo, different branches** - Agents conflict on working directory
2. **Separate clones** - Expensive disk/network, slow setup
3. **Git worktrees** - Lightweight, shared object store, isolated working dirs

## Decision

**Each agent session gets its own git worktree with isolated environment.**

### Worktree Structure

```
/tmp/issue-orchestrator-worktrees/
├── issue-123/           # Worktree for issue #123
│   ├── .git             # Symlink to main repo's git dir
│   ├── .issue-orchestrator/
│   │   ├── completion.json
│   │   └── session.log
│   └── <project files>
├── issue-456/           # Worktree for issue #456
└── review-pr-789/       # Worktree for PR review
```

### Isolation Guarantees

| Aspect | Isolation |
|--------|-----------|
| **File system** | Separate directory per session |
| **Git branch** | Dedicated branch per issue |
| **Git index** | Independent staging area |
| **Environment** | Scrubbed env, isolated HOME |
| **Credentials** | No GitHub tokens (ADR-0005) |

### Lifecycle

1. **Create**: `git worktree add <path> -b <branch>`
2. **Launch**: Start agent session in worktree directory
3. **Monitor**: Watch for completion.json
4. **Cleanup**: `git worktree remove <path>` after completion

### Environment Scrubbing

Each worktree session starts with:
```bash
# Removed from environment
unset GITHUB_TOKEN GH_TOKEN SSH_AUTH_SOCK
unset AWS_* OPENAI_API_KEY GOOGLE_API_KEY

# Isolated HOME (no credential helpers)
export HOME=/tmp/issue-orchestrator-worktrees/issue-123/.home
```

## Consequences

### Positive
- **Parallel work**: Multiple agents, no conflicts
- **Fast setup**: Worktrees share git objects (seconds, not minutes)
- **Clean state**: Each session starts fresh
- **Easy cleanup**: Remove worktree, branch intact

### Negative
- Disk usage (full working copy per session)
- Must track and clean up stale worktrees
- Some tools confused by worktree structure

## Related

- ADR-0005: Agent credential isolation
- ADR-0016: Orchestrator as mediator
- `execution/worktree_adapter.py`: Worktree management
