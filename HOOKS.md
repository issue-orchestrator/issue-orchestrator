# Hook Enforcement Architecture

This document describes the multi-layer hook system that ensures agents cannot bypass safety guardrails.

## Why Hooks Matter

Policy documents (CLAUDE.md, prompts) are suggestions. Agents can forget, ignore, or creatively work around them. Hooks are **enforcement** - technical barriers that block unsafe actions before they execute.

Without hooks, agents will find ways around conventions and the system breaks.

## Hook Layers

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 1: AI Meta-Agent Hooks (best - blocks before execute) │
│   Claude Code: PreToolUse in .claude/settings.json          │
│   Cursor: beforeShellExecution in .cursor/hooks.json        │
│   Copilot CLI: --deny-tool flags                            │
│   Codex CLI: Execpolicy in ~/.codex/config.toml             │
└─────────────────────────────────────────────────────────────┘
                           ↓ if bypassed
┌─────────────────────────────────────────────────────────────┐
│ Layer 2: Git Hooks (bypassable with --no-verify)            │
│   Pre-push: runs tests/linters before push allowed          │
│   Chained wrapper: orchestrator + project hooks             │
└─────────────────────────────────────────────────────────────┘
                           ↓ if bypassed
┌─────────────────────────────────────────────────────────────┐
│ Layer 3: Server-Side (ultimate backstop)                    │
│   GitHub branch protection                                  │
│   Required status checks                                    │
│   Cannot be bypassed by client                              │
└─────────────────────────────────────────────────────────────┘
```

## Hook Inventory

### Orchestrator-Installed Hooks (per worktree)

These are installed automatically by issue-orchestrator when creating worktrees.

| Hook | Type | Location | Purpose |
|------|------|----------|---------|
| Pre-push wrapper | Git | `.git/hooks/pre-push` | Chains project + orchestrator hooks, writes audit trail |
| Pre-push (orchestrator) | Git | `.git/hooks/pre-push.orchestrator` | Validates Agent-Status trailers, blocks test-skipping patterns |
| Stop hook | Claude Code | `.claude/settings.json` | Warns if session exits without `agent-done` |
| gh wrapper | PATH script | `scripts/gh` | Blocks `gh pr create` without auth token |

### Target Project Hooks (project-specific)

These must be set up in the target project. The orchestrator helps install them via `setup`.

| Hook | Type | Location | Purpose | Critical? |
|------|------|----------|---------|-----------|
| PreToolUse (Claude) | Claude Code | `.claude/hooks/block-no-verify.sh` | Blocks `git push --no-verify` at AI level | **YES** |
| beforeShellExecution (Cursor) | Cursor | `.cursor/hooks.json` | Blocks `git push --no-verify` at AI level | **YES** |
| Pre-push | Git | `.githooks/pre-push` | Runs project tests/linters before push | **YES** |
| CLAUDE.md | Policy | `CLAUDE.md` | Documents prohibited actions | Advisory |

## Meta-Agent Support Matrix

| Meta-Agent | Hook Mechanism | Can Block `--no-verify` | Supported |
|------------|----------------|------------------------|-----------|
| Claude Code | `PreToolUse` in `.claude/settings.json` | ✅ Yes (exit 2) | ✅ |
| Cursor (1.7+) | `beforeShellExecution` in `.cursor/hooks.json` | ✅ Yes (`"permission": "deny"`) | ✅ |
| GitHub Copilot CLI | `--deny-tool` flags | ✅ Yes (glob patterns) | ✅ |
| OpenAI Codex CLI | `Execpolicy`, `/approvals` | ✅ Yes | ✅ |
| Gemini CLI | In development | ⚠️ Not yet | ❌ |
| Aider | None (lint only) | ❌ No | ❌ |

**Unsupported meta-agents cannot be used** - without hook enforcement, safety guarantees don't hold.

## What Each Hook Blocks

### PreToolUse / beforeShellExecution (AI Level)

Intercepts commands before the AI can execute them:

```bash
# BLOCKED - exit 2 prevents execution
git push --no-verify
git commit --no-verify -m "message"
git -c core.hooksPath=/dev/null push
gh pr merge 123                          # Agents cannot merge PRs
gh pr merge 123 --squash
gh api repos/owner/repo/pulls/123/merge  # API merge also blocked

# ALLOWED - passes through
git push
git commit -m "message"
gh pr create --title "..."               # Creating PRs is fine
gh pr view 123                           # Viewing PRs is fine
```

### Pre-push Wrapper (Git Level)

Runs when `git push` is invoked (unless `--no-verify` bypasses it):

```bash
#!/bin/bash
# Audit trail - proves wrapper executed
echo "$(date -Iseconds) wrapper-started" >> .git/hooks/pre-push.log

# Run project hook first (their tests/linters)
./pre-push.project "$@"
PROJECT_EXIT=$?
echo "$(date -Iseconds) project-hook exit=$PROJECT_EXIT" >> .git/hooks/pre-push.log

# Run orchestrator hook (trailer validation)
./pre-push.orchestrator "$@"
ORCH_EXIT=$?
echo "$(date -Iseconds) orchestrator-hook exit=$ORCH_EXIT" >> .git/hooks/pre-push.log

# Exit with appropriate code
if [ $PROJECT_EXIT -ne 0 ]; then exit $PROJECT_EXIT; fi
if [ $ORCH_EXIT -ne 0 ]; then exit $ORCH_EXIT; fi
exit 0
```

### Pre-push Orchestrator Hook

Validates agent completion:

- Checks for `Agent-Status:` trailer in latest commit
- Validates required fields based on status
- Blocks test-skipping patterns (`@Disabled`, `@Ignore`, `assumeTrue`)

### gh Wrapper

Blocks unauthorized GitHub CLI operations:

```bash
# BLOCKED - no auth token
gh pr create --title "..." --body "..."

# ALLOWED - agent-done sets ORCHESTRATOR_GH_AUTH
ORCHESTRATOR_GH_AUTH=agent-done-authorized gh pr create ...
```

## Verification Flow

Verification proves hooks are not just installed but **effective**.

### Running Verify

```bash
$ issue-orchestrator verify

[1/5] Creating test branch...
      verify-test-1703019876 ✅

[2/5] Making idempotent change...
      echo "verify-1703019876" >> .verify-canary
      git commit -m "chore: verify hooks" ✅

[3/5] Testing PreToolUse (--no-verify must be blocked)...
      Attempting: git push --no-verify origin verify-test-xxx
      → Exit 2: BLOCKED ✅

[4/5] Testing pre-push hook fires...
      git push origin verify-test-xxx
      Checking audit trail: .git/hooks/pre-push.log
      → wrapper-started ✅
      → project-hook exit=0 ✅
      → orchestrator-hook exit=0 ✅

[5/5] Cleanup...
      git branch -D verify-test-xxx ✅
      git push origin --delete verify-test-xxx ✅ (if pushed)

✅ VERIFIED - Guardrails effective
   Wrote: .issue-orchestrator-verified
```

### Verification Marker

The marker file proves verification ran and passed:

```yaml
# .issue-orchestrator-verified
verified_at: 2024-12-19T10:30:00Z
verified_by: issue-orchestrator v1.2.3
meta_agent: claude-code
hooks_hash: sha256:abc123def456...
signature: sha256(verified_at + hooks_hash + secret)
```

- `hooks_hash`: Hash of all hook files - triggers re-verify if hooks change
- `signature`: Tamper-proof - can't just `touch` the file to skip verify

### Startup Behavior

| Marker State | `skip_verification` | Behavior |
|--------------|---------------------|----------|
| Missing | false (default) | Auto-run verify, block if fails |
| Valid | false (default) | ✅ Start normally |
| Stale (hooks changed) | false (default) | Auto-run verify |
| Invalid signature | false (default) | Auto-run verify |
| Any | true | ⚠️ Start with constant warnings |

## Configuration

```yaml
# .issue-orchestrator.yaml

# Verification config (optional)
verify:
  test_file: ".verify-canary"  # File to modify during verify

# Dangerous overrides (NOT RECOMMENDED)
dangerous:
  skip_verification: true      # Skip verify check on startup
  allow_unsupported_agents: true  # Allow meta-agents without hooks
```

If `skip_verification: true`, the orchestrator will:
- Start anyway
- Print warnings every 10 minutes
- Never stop nagging until you run verify

## Setup Flow

```bash
$ issue-orchestrator setup

[1/4] Which AI meta-agent are you using?
      > Claude Code
      > Cursor
      > Copilot CLI
      > Codex CLI

[2/4] Installing AI-level hook (blocks --no-verify)...

      For Claude Code:
        Created: .claude/hooks/block-no-verify.sh
        Updated: .claude/settings.json

      For Cursor:
        Created: .cursor/hooks/block-no-verify.js
        Updated: .cursor/hooks.json

[3/4] Installing pre-push wrapper...
      Existing hook found: .githooks/pre-push
      Backed up to: .githooks/pre-push.project
      Created wrapper: .githooks/pre-push (chains both hooks)

[4/4] Running verification...
      <full verify flow>

✅ Setup complete - guardrails verified
```

## Hook File Templates

### Claude Code: block-no-verify.sh

```bash
#!/bin/bash
# Block dangerous commands for Claude Code agents
# Exit 2 = BLOCK, Exit 0 = ALLOW

input=$(cat)
command=$(echo "$input" | jq -r '.tool_input.command // ""')

# Block --no-verify bypass attempts
if echo "$command" | grep -qE "git\s+(commit|push).*--no-verify"; then
  echo "BLOCKED: --no-verify is forbidden. Pre-commit hooks must run." >&2
  exit 2
fi

# Block gh pr merge - agents cannot merge PRs
if echo "$command" | grep -qE "gh\s+pr\s+merge"; then
  echo "BLOCKED: Agents cannot merge PRs. Only humans can merge." >&2
  exit 2
fi

# Block gh api merge endpoint
if echo "$command" | grep -qE "gh\s+api\s+.*pulls/[0-9]+/merge"; then
  echo "BLOCKED: Agents cannot merge PRs via API." >&2
  exit 2
fi

exit 0
```

### Claude Code: settings.json addition

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": ".claude/hooks/block-no-verify.sh"
          }
        ]
      }
    ]
  }
}
```

### Cursor: hooks.json

```json
{
  "beforeShellExecution": [
    {
      "command": ".cursor/hooks/block-no-verify.sh",
      "output": "json"
    }
  ]
}
```

Cursor hooks return JSON: `{"permission": "deny", "userMessage": "..."}`

## Audit Trail

The pre-push wrapper writes to `.git/hooks/pre-push.log`:

```
2024-12-19T10:30:00+00:00 wrapper-started
2024-12-19T10:30:05+00:00 project-hook exit=0
2024-12-19T10:30:05+00:00 orchestrator-hook exit=0
```

This proves:
- Git invoked the hook mechanism
- Both project and orchestrator hooks executed
- Exit codes for debugging failures

## Troubleshooting

### "Verification failed: --no-verify was not blocked"

The AI-level hook is missing or misconfigured:

1. Check hook file exists and is executable
2. Check settings.json/hooks.json references it correctly
3. Check the hook script actually exits with code 2 for blocked commands

### "Pre-push hook did not execute"

The git hooks path is misconfigured:

1. Check `git config core.hooksPath` points to correct directory
2. Check hook file is executable: `chmod +x .githooks/pre-push`
3. Check hook file has correct shebang: `#!/bin/bash`

### "Verification marker invalid"

The marker file was tampered with or hooks changed:

1. Re-run `issue-orchestrator verify`
2. If hooks were legitimately updated, this is expected

### Agent bypassed hooks anyway

Check which layer failed:

1. AI-level hook logs (if any)
2. `.git/hooks/pre-push.log` audit trail
3. GitHub PR - was it created through `agent-done`?

If all hooks were in place, file a bug report with the bypass method.
