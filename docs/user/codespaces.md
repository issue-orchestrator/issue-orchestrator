# Codespaces

This repo can run in GitHub Codespaces without changing the normal local
`main.yaml` workflow.

## Included setup

The repo now includes:

- `.devcontainer/devcontainer.json`
- `.issue-orchestrator/config/z-codespaces.yaml`

The Codespaces config pins stable repo-engine ports:

- dashboard: `8080`
- control API: `19081`

The Control Center still runs on `19080`.

## Create the codespace

1. Open the repository on GitHub.
2. Use **Code** -> **Codespaces** -> **Create codespace on main**.
3. Wait for `postCreateCommand` to finish. It runs `make worktree-setup`.

## Set required secrets

Before starting the orchestrator, make sure the codespace has the credentials
you actually plan to use:

- `ISSUE_ORCH_GITHUB_TOKEN`
- `ANTHROPIC_API_KEY` for Claude Code
- `OPENAI_API_KEY` for Codex / OpenAI-backed flows

If you only use one provider, you only need that provider's key.

## Start the Control Center

From the integrated terminal:

```bash
source .venv/bin/activate
python -m issue_orchestrator.entrypoints.control_center --port 19080 --no-browser
```

Codespaces should auto-forward `19080` and open the Control Center URL in your
browser.

## Start the repo engine with the Codespaces config

In Control Center:

1. Select this repository.
2. Choose `z-codespaces.yaml`.
3. Click `Start engine`.

That config uses stable forwarded ports that match the devcontainer:

- Control Center: `19080`
- Repo engine dashboard: `8080`
- Repo engine control API: `19081`

## Direct start without Control Center

If you want to skip Control Center:

```bash
source .venv/bin/activate
issue-orchestrator start --config .issue-orchestrator/config/z-codespaces.yaml --port 8080
```

## Local development

Nothing changes for local Mac development. Keep using:

- `.issue-orchestrator/config/main.yaml`
- your existing local startup flow
