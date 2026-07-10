# Triage Data Sources Contract

This document defines the data sources available to triage agents, how to
access them, and safety rules for their use.

Triage agents operate on **local files only**. The orchestrator pre-fetches
everything you need before the session starts; you never call `gh` or the
GitHub API, and you never mutate GitHub state (comments, labels, issues, PRs).
The orchestrator executes all GitHub operations after you complete.

## Data Sources

### Triage Manifest (Authoritative)

The primary input. The orchestrator writes it into your session directory:

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Manifest | `cat .issue-orchestrator/sessions/*/triage-data/manifest.json` | Which PRs to review, with local file names |
| PR metadata | `cat .../triage-data/pr-{N}-meta.json` | Title, body, branch, author |
| PR diff | `cat .../triage-data/pr-{N}-diff.txt` | Actual code changes |

The manifest is the definitive list of PRs in scope. Review exactly those PRs -
no more, no less. On success the orchestrator labels exactly these PRs.

### Orchestrator Configuration (Authoritative)

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Config file | `cat .issue-orchestrator/config/*.yaml` | Agent definitions, timeouts, label names, review workflow |
| Agent prompts | `cat .prompts/<agent>.md` or path from config | What agents are instructed to do |

### Local Logs and Session Artifacts (Advisory)

Use for investigation; they may be incomplete, rotated, or stale.

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Orchestrator log | `cat ~/.issue-orchestrator.log` | Infrastructure errors, label failures, session lifecycle |
| State file | `cat .issue-orchestrator/state.json` | Session history, pending reviews (may be stale) |
| Session run dirs | `ls .issue-orchestrator/sessions/` | Completion records, validation outcomes |

### Worktree State (Advisory)

| Source | Access | What It Tells You |
|--------|--------|-------------------|
| Git status | `git status` in this worktree | Uncommitted work, branch state |
| Git log | `git log` in this worktree | What was committed for this session |

## Safety Rules

### Never Do

1. **Don't call `gh`** - not for reads, not for writes; all PR data you need is local
2. **Don't post comments, edit labels, or create issues/PRs** - the orchestrator owns all GitHub mutations
3. **Don't merge or approve PRs** - those are human decisions
4. **Don't delete worktrees** - they may contain uncommitted work

### Always Do

1. **Start from the manifest** - it is the definitive review scope
2. **Verify advisory sources against the manifest and config** before drawing conclusions
3. **Make improvements locally** - edit prompts/docs in this worktree and commit; the orchestrator publishes your branch
4. **Report everything else in your completion record** - `coding-done completed --implementation "..." --problems "..."`

## What Happens After You Complete

- `coding-done completed`: the orchestrator adds the configured
  `triage_reviewed_label` (default `triage-reviewed`) to every PR in the
  manifest and publishes any commits on your branch.
- Session failure: manifest PRs get the `triage_failed_label`
  (default `triage-failed`).
- No comments are posted, no other labels are flipped, and no issues are
  created on your behalf.
