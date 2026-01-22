# VS Code Integration

Issue Orchestrator includes a native VS Code extension that talks to the MCP server (`issue-orchestrator-mcp`).

## Install

1. Install the Python package so the MCP entrypoint is on PATH.
2. Build or install the VS Code extension from `packages/vscode`.

## MCP Server

Run directly:

```bash
issue-orchestrator-mcp --repo-root /path/to/repo --auto-start
```

The server exposes MCP tools for status, snapshots, and session controls. The VS Code extension starts it automatically.

## Usage

- Open the **Issue Orchestrator** view from the Activity Bar
- Use command palette:
  - `Issue Orchestrator: Start` / `Stop`
  - `Issue Orchestrator: Pause` / `Resume`
  - `Issue Orchestrator: Open Dashboard`
  - `Issue Orchestrator: Quick Actions`
  - `Issue Orchestrator: Select Config`
  - `Issue Orchestrator: Run Diagnostics`
- Right-click a session to open worktree, PR, logs, or session console

## Settings

See the extension README for configuration keys.

## Extension Tests

`make test-vscode` launches the VS Code Extension Development Host with isolated
test profiles under the system temp directory.
