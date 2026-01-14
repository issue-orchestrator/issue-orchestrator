# Triage Data Sources Contract

This document defines the authoritative data sources available to triage agents, how to access them, and safety rules for their use.

## Data Sources

### GitHub (Authoritative)

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Issue body | `gh issue view {N}` | Original requirements, acceptance criteria |
| Issue comments | `gh issue view {N} --comments` | Agent completion reports, human feedback |
| Issue labels | `gh issue view {N} --json labels` | Current state, agent assignment, blocking status |
| PR diff | `gh pr diff {N}` | Actual code changes |
| PR files | `gh pr view {N} --json files` | Which files were modified |
| PR body | `gh pr view {N}` | Agent's description of changes |
| PR status | `gh pr view {N} --json state,mergeable,statusCheckRollup` | CI status, merge readiness |
| PR comments | `gh pr view {N} --comments` | Review feedback, discussions |
| Repo labels | `gh label list --json name` | Available labels for workflow |

### Orchestrator Configuration (Authoritative)

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Config file | `cat .issue-orchestrator/config/*.yaml` | Agent definitions, timeouts, label names, review workflow |
| Agent prompts | `cat .issue-orchestrator/prompts/<agent>.md` or path from config | What agents are instructed to do |
| Agent protocol | `cat AGENT_PROTOCOL.md` | How agents should signal completion |

### Local Logs (Advisory)

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Orchestrator log | `cat ~/.issue-orchestrator.log` | Infrastructure errors, label failures, session lifecycle |
| State file | `cat .issue-orchestrator/state.json` | Session history, pending reviews (may be stale) |
| Claude logs | `ls ~/.claude/projects/-Users-*-dev-<repo>-<issue>/` | Agent decisions, tool calls, errors |

### Terminal Sessions (Advisory)

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| tmux windows | Named `issue-{N}` or `review-{N}` | Real-time terminal output (if still open) |
| tmux sessions | `tmux list-sessions` | Active sessions, may have scrollback |

### Worktree State (Advisory)

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Worktree path | Check config `worktrees.base` + issue number | Local file state, uncommitted changes |
| Git status | `git status` in worktree | Uncommitted work, branch state |
| Completion file | `cat completion.json` in worktree | Agent's reported outcome |

## Reliability Tiers

### Authoritative Sources
These are the source of truth. Trust them over advisory sources.

- **GitHub labels** - Definitive issue/PR state
- **GitHub PR state** - Merge status, CI status
- **Orchestrator config** - What agents are configured to do
- **Agent prompts** - What agents are instructed to do

### Advisory Sources
Use for investigation, but verify against authoritative sources.

- **Local logs** - May be incomplete, rotated, or from old sessions
- **State file** - Snapshot in time, may not reflect current GitHub state
- **Cached data** - Orchestrator caches issue lists; may be stale
- **Terminal sessions** - May have closed, scrollback may be truncated

## Safety Rules

### Never Do

1. **Don't merge PRs** - Merging is a human decision
2. **Don't modify agent prompts** - Unless triage explicitly asks for prompt fixes
3. **Don't trust cache without observation** - Always verify against GitHub
4. **Don't remove blocking labels** - Only humans unblock issues
5. **Don't delete worktrees** - May contain uncommitted work

### Always Do

1. **Verify GitHub state first** - Labels, PR status, CI checks
2. **Read issue comments** - Contains agent completion reports
3. **Check orchestrator log for infrastructure issues** - Before blaming agents
4. **Create issues for fixes** - Don't directly implement triage recommendations
5. **Mark triage-created issues as `blocked`** - Requires human approval

## Common Queries

### Find failed issues
```bash
gh issue list --label "blocked-failed" --json number,title
```

### Find issues needing review
```bash
gh pr list --label "needs-code-review" --json number,title,url
```

### Check orchestrator health
```bash
grep -E "(FAILED|BLOCKED|ERROR)" ~/.issue-orchestrator.log | tail -20
```

### Find repeated failures (red flag)
```bash
grep "FAILED" ~/.issue-orchestrator.log | awk '{print $NF}' | sort | uniq -c | sort -rn | head -10
```

### Get agent completion report
```bash
gh issue view {N} --comments | grep -A 50 "## Completion"
```

### Check if required labels exist
```bash
gh label list --json name --jq '.[].name' | grep -E "^(in-progress|blocked|needs-code-review)$"
```

## Workflow Integration

Triage agents should:

1. **Gather facts** from authoritative sources first
2. **Correlate** with advisory sources for investigation
3. **Create issues** with `blocked` + `triage-fix` labels for recommended fixes
4. **Wait for human approval** - humans remove `blocked` to authorize work
5. **Report findings** - Comment on issues/PRs with analysis

This ensures humans remain in control of process changes and scheduling.
