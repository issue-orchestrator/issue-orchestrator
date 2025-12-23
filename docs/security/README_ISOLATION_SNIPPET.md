## Agent isolation modes

Issue Orchestrator supports two isolation modes:

- **Standard (default)**: agents run with a scrubbed environment and isolated HOME to prevent accidental GitHub authentication.
- **Hardened (opt-in)**: agents run under a dedicated low-privilege OS user with no credentials, preventing GitHub API access by construction.

Both modes run a quick sandbox verification on worktree/session start.

See `SECURITY_ISOLATION.md`.
