# Installation

## Prerequisites

- **Python 3.11+** (3.14 recommended; the build auto-detects the best available version)
- **[uv](https://docs.astral.sh/uv/)** — used for dependency management and lockfile resolution
- **GNU Make** — `make` on Linux, `gmake` on macOS (`brew install make`)
- **Git** — for worktree management
- **GitHub CLI (`gh`)** or a GitHub personal access token — for API access

## Install

```bash
git clone https://github.com/BruceBGordon/issue-orchestrator.git
cd issue-orchestrator
make venv
```

`make venv` creates a `.venv` with the best available Python, installs all dependencies from `uv.lock`, and installs the `issue-orchestrator` CLI as an editable package.

Activate the environment:

```bash
source .venv/bin/activate
```

Verify the install:

```bash
issue-orchestrator --help
```

## GitHub token

The orchestrator needs a GitHub token with repo access. Set it via environment variable:

```bash
export ISSUE_ORCH_GITHUB_TOKEN=ghp_...
```

Or store it in the OS keychain:

```bash
issue-orchestrator auth store
```

See [GitHub Permissions](github-permissions.md) for required scopes and alternative auth methods.

## Next steps

Run `issue-orchestrator setup` to create a config file and initialize your project, then see the [Quickstart Guide](quickstart.md).
