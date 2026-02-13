# ADR 0012: Mechanical guardrails over policy documents

**Status:** Accepted
**Date:** 2024-12-21

## Context

AI agents are untrusted. They can and will:
- Forget instructions in CLAUDE.md
- Creatively work around conventions
- Use `--no-verify` to skip hooks
- Attempt direct merges or credential access

Policy documents (CLAUDE.md, prompts) are suggestions. Relying on "agent good behavior" is insufficient for safety-critical invariants.

## Decision

**Enforce invariants mechanically. Hooks block; policy documents inform.**

### Enforcement Layers

1. **AI Meta-Agent Hooks** (best - blocks before execution)
   - Claude Code: `PreToolUse` in `.claude/settings.json`
   - Cursor: `beforeShellExecution` in `.cursor/hooks.json`
   - Exit code 2 = BLOCK

2. **Git Hooks** (bypassable with `--no-verify`, hence layer 1)
   - Pre-push: runs tests/linters before push
   - Chained wrapper: orchestrator + project hooks

3. **Server-Side** (ultimate backstop)
   - GitHub branch protection
   - Required status checks
   - Cannot be bypassed by client

### What Gets Blocked

- `git push --no-verify` - blocked at AI level
- `git commit --no-verify` - blocked at AI level
- `gh pr merge` - agents cannot merge PRs
- `gh api .../merge` - API merge also blocked
- Credential access - sandbox verification at session start

## Consequences

### Positive
- System invariants survive adversarial agent behavior
- Clear security story
- Reduced chance of silent bad merges
- Unsupported AI agents (no hooks) are rejected

### Negative
- Setup complexity (multiple hook layers)
- Must maintain hooks for each supported AI tool
- Verification step adds startup latency

## Related

- ADR-0005: Human merge and agent credential isolation
- `docs/architecture/hooks.md`: Detailed hook implementation
