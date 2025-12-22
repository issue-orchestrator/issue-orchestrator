**Audience:** Design document (public). Not a usage guide.

# Security and Isolation Modes

## Standard (default, no sudo)
- Agents run as the current OS user
- Orchestrator launches agent sessions with a scrubbed environment and an isolated HOME per worktree (recommended)
- A fast **affirmative sandbox verification** runs at worktree/session start

## Hardened (opt-in, sudo once)
- Agents run under a dedicated low-privilege OS user with no credentials
- Prevents GitHub API access by construction

## Sandbox verification (both modes)
Minimum checks:
- `gh auth status` must fail
- `git push --dry-run` must fail fast (no prompt)
- forbidden env vars absent (`GITHUB_TOKEN`, `GH_TOKEN`, `SSH_AUTH_SOCK`, etc.)
- mode-specific: HOME isolated (standard) or `whoami` is sandbox user (hardened)
