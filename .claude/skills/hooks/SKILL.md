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
- [Hook Architecture](../../../docs/architecture/hooks.md) - Complete hook architecture documentation
- `hooks/` - Repo-local git hook templates used in this repo
- `.claude/hooks/` - Repo-local Claude hook wiring for this repo
- `src/issue_orchestrator/infra/hooks/` - AI hook installation and verification adapters
- `src/issue_orchestrator/infra/repo_guardrails.py` - Target repo guardrail installer for `setup-guardrails`
- `src/issue_orchestrator/templates/hooks/` - Managed hook templates for target repos
- `src/issue_orchestrator/hooks/pre-push` - Bundled orchestrator pre-push hook installed in worktrees
- `src/issue_orchestrator/adapters/worktree/_worktree_hooks.py` - Per-worktree git hook chaining and Python path bake-in

## Hook Layers (Defense in Depth)

```
Layer 1: AI Meta-Agent Hooks (best - blocks before execute)
  └─ Claude Code: PreToolUse in .claude/settings.json
  └─ Cursor: beforeShellExecution in .cursor/hooks.json
  └─ Copilot: hook JSON + deny tooling
  └─ Codex: Execpolicy rules

Layer 2: Git Hooks (bypassable with --no-verify)
  └─ Repo-local pre-push: scripts/verify-pr.sh via setup-guardrails
  └─ Worktree pre-push wrapper: project hook first, orchestrator hook second
  └─ Orchestrator hook: dirty-tree guard + agent-specific push checks

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
- dirty-tree guard via `issue_orchestrator.entrypoints.cli_tools.prepush_check --dirty-only`
- `make validate-pr` - Required PR gate (`validate` + agent-backed simulated/integration slices)

### `src/issue_orchestrator/hooks/pre-push`
Installed into orchestrator-created worktrees:
- resolves the orchestrator Python interpreter (`ISSUE_ORCHESTRATOR_PYTHON` or baked-in `sys.executable`)
- runs the dirty-tree guard
- blocks target-repo test-skipping patterns such as `@Disabled`, `@Ignore`, `assumeTrue`, `assumeFalse`

### Installation
```bash
cp hooks/pre-commit .git/hooks/pre-commit
cp hooks/pre-push .git/hooks/pre-push
chmod +x .git/hooks/pre-*
```

Or use: `git config core.hooksPath hooks`.

For managed target repos, prefer:
```bash
issue-orchestrator setup-guardrails
```

Use `issue-orchestrator setup-hooks` only when intentionally installing AI hook wiring without repo-local pre-push guardrails.

## What Gets Blocked

| Command | Layer | Why |
|---------|-------|-----|
| `git push --no-verify` | AI hook | Bypasses all git hooks |
| `git commit --no-verify` | AI hook | Bypasses pre-commit |
| `gh pr merge` | AI hook | Agents cannot merge PRs |
| Push with failing tests | Git hook | Repo-local pre-push runs `scripts/verify-pr.sh` |
| Push with dirty tracked files | Git hook | Dirty-tree guard reads `validation.publish.dirty_check` |
| Push with import violations | Git hook | Pre-commit runs lint-imports |

## Modifying Hooks

### Adding a New AI Block Rule
1. Edit the template under `src/issue_orchestrator/templates/hooks/<agent>/`.
2. Update the matching adapter/tests in `src/issue_orchestrator/infra/hooks/`.
3. For this repo's own Claude hook copy, update `.claude/hooks/` if the repo-local copy must change too.
4. Test the hook manually with the adapter's input format and run the relevant hook tests.

### Changing Pre-push Validation
1. For this repo's local publish gate, edit `hooks/pre-push` or the Makefile target it calls.
2. For target-repo managed guardrails, edit `src/issue_orchestrator/infra/repo_guardrails.py` and `src/issue_orchestrator/templates/hooks/git/`.
3. For worktree-installed checks, edit `src/issue_orchestrator/hooks/pre-push` and `_worktree_hooks.py`.
4. Test locally with `./hooks/pre-push`, `make validate-pr`, and focused unit tests.

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

# Check repo-local pre-push
./hooks/pre-push
```

### Tests Passing Locally But Hook Fails
The hook runs `make validate` which may have stricter checks than individual test runs.
