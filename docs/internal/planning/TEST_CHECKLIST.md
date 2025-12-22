# What to Test (checklist)

Prefer fast tests and isolate OS-dependent checks.

## Unit tests
### Validation
- [ ] record writer writes JSON with exact suite + SHA
- [ ] cache lookup hit requires exact suite + SHA
- [ ] cache miss when suite differs or SHA differs
- [ ] runner enforces timeout

### Env scrubbing
- [ ] build_agent_env removes GH_TOKEN/GITHUB_TOKEN/SSH_AUTH_SOCK
- [ ] build_agent_env sets HOME to worktree-local agent_home
- [ ] build_agent_env sets GIT_TERMINAL_PROMPT=0 and GIT_ASKPASS=/usr/bin/false

## Integration tests (fast; allow skipping on CI if needed)
- [ ] subprocess in agent env: `gh auth status` exits non-zero
- [ ] `git push --dry-run` exits non-zero quickly (timeout <= 2s)

## Contract tests
- [ ] publish gate blocks push when validation fails
- [ ] publish gate proceeds on cache hit for same SHA
- [ ] agent_done blocks completion when agent_gate fails (when configured)
- [ ] pre-push exits 0 when cache exists (no re-run)

## Manual smoke tests
- [ ] new worktree + agent session: verify passes
- [ ] inject creds leakage: verify fails and blocks session
- [ ] validate once: publish reuses cache and does not rerun
