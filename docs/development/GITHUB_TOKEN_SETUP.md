# GitHub Token Setup (Developer)

How the orchestrator resolves GitHub tokens at runtime. For creating a token and required scopes, see [GitHub Permissions (User Guide)](../user/github-permissions.md).

The system does not shell out to the GitHub CLI for token discovery, but it does read GitHub CLI auth from `hosts.yml`. There are two supported patterns:

1) Global auth sources
2) Repo-scoped auth sources declared in config

## Rotate an Expiring Token

When GitHub sends a personal access token expiration notice, first identify the
auth source the target repo actually uses. Check the selected config file, such
as `.issue-orchestrator/config/default.yaml`, and inspect `repo.github`.

If GitHub offers **generate an equivalent**, use that first. Otherwise create a
new fine-grained token with the permissions listed in the user guide.

For repo-scoped auth, the config is authoritative. Resolution order is:

`repo.github.token` > `repo.github.token_env` > `repo.github.keyring_service` / `keyring_username`

Rotation steps:

1. If `repo.github.token_env` is set and that environment variable is present
   in the Control Center or orchestrator process, update that environment
   variable and restart the process. A stale env var wins over Keychain.
2. If `repo.github.keyring_service` / `keyring_username` is configured, replace
   that exact OS keychain item. Expand `${USER}` before using it.
3. If the repo has no repo-scoped auth, rotate the global source it uses
   instead: exported env var, GitHub CLI auth, or the default keychain entry
   from `issue-orchestrator auth store`.
4. Run `issue-orchestrator --config <config-path> doctor` from the target repo.
   Confirm **Token Sources** names the expected source and **GitHub Auth**
   confirms access to `repo.name`.
5. Restart any already-running repository engine or Control Center-launched
   engine so it reloads the credential.

On macOS, replace a repo-scoped Keychain entry without putting the token in
shell history:

```bash
KEYRING_SERVICE="tixmeup-github"
KEYRING_USERNAME="$USER"
old_stty="$(stty -g)"
trap 'stty "$old_stty"; unset token' EXIT

printf "New GitHub PAT: "
stty -echo
IFS= read -r token
stty "$old_stty"
printf "\n"

security add-generic-password -U \
  -s "$KEYRING_SERVICE" \
  -a "$KEYRING_USERNAME" \
  -w "$token"

trap - EXIT
unset token old_stty
```

Then verify, and restart any engine that is already running:

```bash
issue-orchestrator --config .issue-orchestrator/config/default.yaml doctor
issue-orchestrator --config .issue-orchestrator/config/default.yaml restart
```

Do not use `issue-orchestrator auth store` to update a repo-scoped Keychain
entry. That command writes the global fallback entry (`issue-orchestrator` /
`github-token`), not a service like `tixmeup-github`.

## Global Auth Sources

Set a token in your shell. The global fallback order is:

`ISSUE_ORCH_GITHUB_TOKEN` > `GITHUB_TOKEN` > `GH_TOKEN` > GitHub CLI `hosts.yml` > the default keyring entry created by `issue-orchestrator auth store`

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
