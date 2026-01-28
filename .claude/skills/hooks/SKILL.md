---
name: hooks
description: Git hooks and AI meta-agent hooks for enforcement. Use when working on hooks/, .claude/hooks/, pre-push validation, or understanding the multi-layer hook system.
---

# Hooks Skill

This skill provides context for working with the multi-layer hook enforcement system.

## When to Use

- Working on files in `hooks/` directory
- Modifying `.claude/hooks/` scripts
- Understanding hook installation or verification
- Debugging why a hook blocked something
- Adding new hook enforcement rules

## Key Resources

Read these for full context:
- [docs/architecture/hooks.md](docs/architecture/hooks.md) - Complete hook architecture documentation
- `hooks/` - Local git hook templates (pre-commit, pre-push)
- `.claude/hooks/` - AI meta-agent hooks (block-no-verify.sh)
- `src/issue_orchestrator/infra/hooks.py` - Hook installation logic

## Hook Layers (Defense in Depth)

```
Layer 1: AI Meta-Agent Hooks (best - blocks before execute)
  └─ Claude Code: PreToolUse in .claude/settings.json
  └─ Cursor: beforeShellExecution in .cursor/hooks.json

Layer 2: Git Hooks (bypassable with --no-verify)
  └─ Pre-commit: import-linter + AST guardrails
  └─ Pre-push: make validate (tests, types, linters)

Layer 3: Server-Side (ultimate backstop)
  └─ GitHub branch protection
  └─ Required status checks
```

## This Repo's Hooks

### `hooks/pre-commit`
Runs before `git commit`:
- `lint-imports` - Architecture dependency checks
- `check_arch_guardrails.py` - AST-based guardrails

### `hooks/pre-push`
Runs before `git push`:
- `make validate` - Full validation (tests, pyright, linters)

### Installation
```bash
cp hooks/pre-commit .git/hooks/pre-commit
cp hooks/pre-push .git/hooks/pre-push
chmod +x .git/hooks/pre-*
```

Or use: `git config core.hooksPath hooks`

## What Gets Blocked

| Command | Layer | Why |
|---------|-------|-----|
| `git push --no-verify` | AI hook | Bypasses all git hooks |
| `git commit --no-verify` | AI hook | Bypasses pre-commit |
| `gh pr merge` | AI hook | Agents cannot merge PRs |
| Push with failing tests | Git hook | Pre-push runs make validate |
| Push with import violations | Git hook | Pre-commit runs lint-imports |

## Modifying Hooks

### Adding a New Block Rule
1. Edit `.claude/hooks/block-no-verify.sh`
2. Add grep pattern and exit 2 for blocked commands
3. Test: run the hook manually with test input

### Changing Pre-push Validation
1. Edit `hooks/pre-push` or the Makefile targets it calls
2. Test locally: `./hooks/pre-push` or `make validate`

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Allow (command proceeds) |
| 1 | Error (hook failed, shows error) |
| 2 | Block (Claude Code: command denied) |

## Troubleshooting

### Hook Not Running
```bash
# Check hooks path
git config core.hooksPath

# Check permissions
ls -la .git/hooks/
ls -la hooks/
```

### Hook Running But Not Blocking
```bash
# Test AI hook manually
echo '{"tool_input":{"command":"git push --no-verify"}}' | .claude/hooks/block-no-verify.sh
echo $?  # Should be 2

# Check pre-push
./hooks/pre-push
```

### Tests Passing Locally But Hook Fails
The hook runs `make validate` which may have stricter checks than individual test runs.
