# Getting Started

You want to try Issue Orchestrator — get it running, see what it does, then decide if it's for you.

## Before you start

You'll need:
- Python 3.11+, [uv](https://docs.astral.sh/uv/), GNU Make, Git
- A GitHub token with repo access
- **An AI coding tool**: Claude Code, Cursor, or Codex CLI. The orchestrator launches these to work on issues. If you don't have one installed, nothing will run.

## 1. Install

```bash
git clone https://github.com/BruceBGordon/issue-orchestrator.git
cd issue-orchestrator
make venv
source .venv/bin/activate
```

Full details: [Installation](../user/installation.md)

## 2. Run the setup wizard

```bash
issue-orchestrator setup
```

The wizard asks about your repo, agents, validation, and review preferences. If you're exploring, pick the "Explore" path — it minimizes setup. You can always re-run the wizard later with production settings.

The wizard creates `.issue-orchestrator/config/default.yaml`. You can also write this by hand — see [Configuration](../user/configuration.md) for a minimal starter config.

## 3. Install safety hooks

```bash
issue-orchestrator setup-hooks
```

Hooks prevent agents from bypassing validation (e.g., `git push --no-verify`). This is important — without hooks, agents can skip your tests. See [Guardrails](../design/guardrails.md) for why this matters.

## 4. Label a GitHub issue

Add an agent label (e.g., `agent:dev`) to a GitHub issue in your repo. The orchestrator picks up issues by label — no label, no work.

## 5. Start the orchestrator

```bash
issue-orchestrator start
```

Open `http://localhost:8080` to watch the dashboard. You'll see your issue move through the pipeline: queued, running, review, done.

## 6. Verify everything works

```bash
issue-orchestrator doctor
```

Doctor checks GitHub connectivity, token permissions, config validity, and hook installation.

## What happens under the hood

The orchestrator:
1. Fetches labeled issues from GitHub
2. Creates an isolated git worktree for each issue
3. Launches your AI tool in that worktree with a prompt
4. Waits for the agent to call `agent-done` (the structured completion command)
5. Runs validation (tests, linting)
6. Queues code review if configured
7. Creates a PR when everything passes
8. You merge

For the full walkthrough with examples, prompt templates, and multi-agent setup, read the [Tutorial](../user/tutorial.md).

## What to read next

| If you want to... | Read |
|---|---|
| Understand all config options | [Configuration](../user/configuration.md) then [Configuration Reference](../user/configuration_reference.md) |
| Set up code review | [Review Workflow](../development/REVIEW_WORKFLOW.md) |
| Use the VS Code extension | [VS Code Integration](../user/vscode.md) |
| Run E2E tests automatically | [E2E Runner](../user/e2e.md) |
| Common questions | [FAQ](../user/faq.md) |
| Something broke | [Troubleshooting](../development/TROUBLESHOOTING.md) |
