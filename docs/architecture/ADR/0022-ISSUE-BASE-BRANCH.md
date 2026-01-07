# ADR: Issue-specific base branch (default main)

**Status:** Proposed  
**Date:** 2026-01-05

## Context
Issue-orchestrator currently assumes all work targets the `main` branch.
Some workflows require release or stabilization branches.

Unconstrained base branches introduce complexity in dependencies,
worktrees, PRs, and reconciliation.

## Decision
Support an optional issue-specific base branch with strict constraints.

### Default
- Base branch defaults to `main`.

### Constraints
1. Dependencies must share the same base branch.
2. Base branch is immutable once work starts.
3. PR base must match issue base branch.

## Metadata format
```
io:
  base-branch: release/1.2
```

## Enforcement
- Invalid cross-branch dependencies block the issue.
- Apply label: `blocked:dependency-cross-base-branch`.

## Consequences
- Supports real workflows while preserving clarity.
