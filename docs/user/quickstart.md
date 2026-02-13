# Quickstart

This guide gets you from zero to a running orchestrator. For prerequisites and install details, see [Installation](installation.md).

## 1. Install and activate

```bash
make venv
source .venv/bin/activate
```

## 2. Set your GitHub token

```bash
export ISSUE_ORCH_GITHUB_TOKEN=ghp_...
```

The token needs `repo` scope. See [GitHub Permissions](github-permissions.md) for details.

## 3. Run the setup wizard

```bash
issue-orchestrator setup
```

This creates `.issue-orchestrator/config/default.yaml` with your repo settings and agent definitions. You can also copy and edit the [example config](../../examples/config.example.yaml) directly.

A minimal config looks like:

```yaml
agents:
  "agent:dev":
    prompt: ".issue-orchestrator/prompts/dev.md"
    model: "sonnet"
    ai_system: "claude-code"

validation:
  cmd: "make test"
  timeout_seconds: 300
```

## 4. Label a GitHub issue

Add the `agent:dev` label to a GitHub issue in your repo. The orchestrator picks up issues by label.

## 5. Start the orchestrator

```bash
issue-orchestrator start
```

This launches the orchestrator with the web dashboard. Open `http://localhost:8080` to watch issues move through the pipeline.

## 6. Verify it's working

```bash
issue-orchestrator doctor
```

Doctor checks GitHub connectivity, token permissions, config validity, and hook installation.

## What happens next

The orchestrator will:

1. Fetch labeled issues from GitHub
2. Create isolated git worktrees for each issue
3. Launch agent sessions to work on the code
4. Run validation (`make validate`) on completed work
5. Queue code review (if configured)
6. Create a draft PR when review passes
7. Wait for a human to merge

See [Configuration](configuration.md) for the full set of options, or the [Configuration Reference](configuration_reference.md) for every field.
