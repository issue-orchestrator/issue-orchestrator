# GitHub Token Setup (Developer)

How the orchestrator resolves GitHub tokens at runtime. For creating a token and required scopes, see [GitHub Permissions (User Guide)](../user/github-permissions.md).

The system does not use the GitHub CLI for token discovery. Two supported options:

1) Environment variable (recommended for simplicity)
2) macOS keychain + `hosts.yml` (recommended for safer storage)

## Option 1: Environment Variable

Set a token in your shell. The resolution order is `ISSUE_ORCH_GITHUB_TOKEN` > `GITHUB_TOKEN` > `GH_TOKEN`.

```bash
export ISSUE_ORCH_GITHUB_TOKEN="ghp_..."
```

Optional config:

```yaml
repo:
  github:
    token_env: GITHUB_TOKEN
```

Notes:
- This keeps tokens out of repo files.
- Use your shell profile if you want it to persist.

## Option 2: macOS Keychain + hosts.yml

This uses a keychain entry plus a lightweight `hosts.yml` file for the GitHub username.
No `gh` CLI is required.

1) Create `hosts.yml` with your GitHub username:

```yaml
github.com:
  user: <your-github-username>
```

Save it at one of:
- `~/.config/gh/hosts.yml`
- `~/Library/Application Support/gh/hosts.yml`

2) Add the token to the macOS keychain:

```bash
security add-generic-password \
  -s gh:github.com \
  -a <your-github-username> \
  -w "ghp_..."
```

3) (Optional) Remove the token from shell envs to avoid duplication:

```bash
unset GITHUB_TOKEN
unset GH_TOKEN
```

Notes:
- The system reads the username from `hosts.yml` and the token from keychain.
- If `hosts.yml` contains an `oauth_token` or `token` entry, it will use that too,
  but the keychain path is preferred for safety.
