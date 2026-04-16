# GitHub Token Setup (Developer)

How the orchestrator resolves GitHub tokens at runtime. For creating a token and required scopes, see [GitHub Permissions (User Guide)](../user/github-permissions.md).

The system does not use the GitHub CLI for token discovery. There are two supported patterns:

1) Global auth sources
2) Repo-scoped auth sources declared in config

## Global Auth Sources

Set a token in your shell. The global fallback order is:

`ISSUE_ORCH_GITHUB_TOKEN` > `GITHUB_TOKEN` > `GH_TOKEN` > the default keyring entry created by `issue-orchestrator auth store`

```bash
export ISSUE_ORCH_GITHUB_TOKEN="ghp_..."
```

You can also store the global fallback token in the default keyring entry:

```bash
issue-orchestrator auth store
```

## Repo-Scoped Auth Sources

When a repo declares its own GitHub auth source in config, that source becomes authoritative.
The orchestrator does not silently fall back to a different global token.

Example:

```yaml
repo:
  name: "BruceBGordon/tixmeup"
  github:
    token_env: TIXMEUP_GITHUB_TOKEN
    keyring_service: tixmeup-github
    keyring_username: "${USER}"
```

Notes:
- `token_env` lets a repo demand a specific env var.
- `keyring_service` and `keyring_username` let a repo demand a specific keyring entry.
- You can declare one or both. Resolution order within repo-scoped mode is:
  `repo.github.token` > `repo.github.token_env` > `repo.github.keyring_service` / `keyring_username`

On macOS you can create a matching generic password entry like this:

```bash
security add-generic-password \
  -s tixmeup-github \
  -a "$USER" \
  -w "ghp_..."
```

Notes:
- `doctor` validates access to the configured `repo.name`, not just a generic GitHub `/user` check.
- Control Center prereqs/start use the same repo-scoped auth logic as the runtime adapter.
- Control Center starts repository engines directly through the orchestrator
  supervisor. It does not run target-repo wrapper scripts, so any script-only
  export of `TIXMEUP_GITHUB_TOKEN` is bypassed unless that variable is already
  present in the Control Center process environment.
- For Control Center launches, declare the durable fallback in the repo config
  itself with `keyring_service` and `keyring_username` when you want macOS
  Keychain auth to work without exporting the token manually.
