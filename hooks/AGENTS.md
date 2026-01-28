# Git Hook Templates

This directory contains git hook templates for local development.

## Files

| File | Purpose |
|------|---------|
| `pre-commit` | Runs import-linter + AST guardrails before commit |
| `pre-push` | Runs `make validate` (tests, pyright, linters) before push |

## Installation

```bash
# Option 1: Copy to .git/hooks
cp hooks/pre-commit .git/hooks/pre-commit
cp hooks/pre-push .git/hooks/pre-push
chmod +x .git/hooks/pre-*

# Option 2: Set hooks path (applies to all hooks in this directory)
git config core.hooksPath hooks
```

## Modifying Hooks

When editing these hooks:

1. **Keep them fast** - Pre-commit especially should be quick
2. **Exit codes matter**:
   - `0` = success, allow operation
   - Non-zero = failure, block operation
3. **Test locally** before committing: `./hooks/pre-commit` or `./hooks/pre-push`
4. **Mirror CI** - The pre-push hook should run the same checks as CI

## Related Documentation

For the full hook architecture (AI-level hooks, verification, etc.):
- [docs/architecture/hooks.md](../docs/architecture/hooks.md)

For the hooks skill (auto-invoked when working here):
- `.claude/skills/hooks/SKILL.md`
