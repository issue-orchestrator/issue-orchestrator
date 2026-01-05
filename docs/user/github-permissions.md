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

### Option 2: Classic PAT

1. Go to https://github.com/settings/tokens/new
2. Select `repo` scope

## Agent Credentials

**Agents get no GitHub token.** The orchestrator handles all GitHub operations.

See [ADR-0005](../architecture/ADR/0005-human-merge-and-agent-credential-isolation.md) and [ADR-0016](../architecture/ADR/0016-orchestrator-as-mediator.md) for why.
