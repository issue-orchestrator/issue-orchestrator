# verify-agent-sandbox spec

`issue-orchestrator verify-agent-sandbox` should be runnable:
- directly (for debugging)
- automatically during worktree/session bootstrap

## Inputs
- worktree path
- expected mode: Standard or Hardened

## Checks (minimum)
1) Forbidden env vars absent:
   - `GITHUB_TOKEN`, `GH_TOKEN`, `SSH_AUTH_SOCK`
2) `gh auth status` fails (exit non-zero)
3) `git push --dry-run` fails fast (exit non-zero) and does not prompt
   - ensure `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=/usr/bin/false` are in effect
4) Mode-specific:
   - Standard: HOME is isolated under `.issue-orchestrator/agent_home`
   - Hardened: `whoami` equals sandbox user (e.g., `issueorch-agent`)

## Output
- human-readable summary
- structured JSON output option for orchestration/web UI:
  - pass/fail, failing checks, remediation suggestions

## Failure behavior
If used during bootstrap:
- refuse to start agent session
- emit trace event (e.g., `sandbox_verification_failed`)
- surface remediation instructions (e.g., "disable gh auth in agent HOME")
