# Issue Orchestrator VS Code Extension

Native VS Code integration for issue-orchestrator. The extension connects to the MCP server (`issue-orchestrator-mcp`) and renders orchestrator state inside VS Code.

## Features

- Activity bar view with Active Sessions, Queue, Blocked, History, Reviews, and Diagnostics
- Start/stop/pause/resume/refresh orchestrator commands
- Open dashboard in a VS Code webview or external browser
- Open worktrees, issues, PRs, and logs directly from VS Code
- Issue detail panels with phase/manifest context
- Session console with Claude log tail + send input
- Problems panel shows blocked/dependency issues
- Focus or kill sessions from the tree view
- Stream events for quick updates

## Quick Start

1. Install the main Python package and ensure `issue-orchestrator-mcp` is on your PATH.
2. Configure your repo as usual (`.issue-orchestrator/config/default.yaml`).
3. In VS Code, set `issueOrchestrator.repoRoot` if your workspace root is different.
4. Run **Issue Orchestrator: Start**.

## Settings

- `issueOrchestrator.repoRoot`: Repo root (defaults to first workspace folder)
- `issueOrchestrator.configPath`: Config file path (optional)
- `issueOrchestrator.instanceId`: Multi-instance orchestrator id
- `issueOrchestrator.mcpCommand`: MCP server command
- `issueOrchestrator.pollIntervalSeconds`: Poll interval in seconds
- `issueOrchestrator.autoStart`: Start orchestrator if not running
- `issueOrchestrator.notifications.sessionCompleted`: completion notifications
- `issueOrchestrator.notifications.sessionFailed`: failure notifications
- `issueOrchestrator.notifications.sessionBlocked`: blocked notifications
- `issueOrchestrator.selectConfig`: pick a config file
- `issueOrchestrator.runDiagnostics`: render doctor report

## Development

```bash
cd packages/vscode
npm install
npm run compile
```

Launch with VS Code's extension host or package with `vsce`.
