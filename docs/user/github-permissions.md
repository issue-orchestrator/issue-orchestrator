# GitHub Token Setup

## Quick Start

```bash
export ISSUE_ORCH_GITHUB_TOKEN=ghp_xxxxxxxxxxxx
```

## Creating a Token

### Option 1: Fine-Grained PAT (Recommended)

1. Go to https://github.com/settings/personal-access-tokens/new
2. Select the repository
3. Set permissions:

| Permission | Access |
|------------|--------|
| Contents | Read and write |
| Issues | Read and write |
| Pull requests | Read and write |
| Metadata | Read (automatic) |

## Rotating an Expiring Token

If GitHub warns that a token is expiring, generate an equivalent token when
GitHub offers that option. Then update the auth source that issue-orchestrator
actually uses:

- exported env var: replace the variable and restart the process
- repo-scoped Keychain entry: replace the configured service/account entry
- global keychain fallback: rerun `issue-orchestrator auth store`
- GitHub CLI auth: refresh the relevant `gh auth` login

Run `issue-orchestrator --config <config-path> doctor` afterward and confirm it
authenticates to the target repo. For the exact resolution order and Keychain
commands, see [GitHub Token Setup (Developer)](../development/GITHUB_TOKEN_SETUP.md#rotate-an-expiring-token).

### Option 2: Classic PAT

1. Go to https://github.com/settings/tokens/new
2. Select `repo` scope

## Agent Credentials

**Agents get no GitHub token.** The orchestrator handles all GitHub operations.

See [ADR-0005](../architecture/ADR/0005-human-merge-and-agent-credential-isolation.md) and [ADR-0016](../architecture/ADR/0016-orchestrator-as-mediator.md) for why.

## Token Resolution Details

For the full resolution chain (env var, GitHub CLI `hosts.yml`, keychain) and alternative storage options, see [GitHub Token Setup (Developer)](../development/GITHUB_TOKEN_SETUP.md).
