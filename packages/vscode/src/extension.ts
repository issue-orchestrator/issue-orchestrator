import * as vscode from "vscode";
import * as path from "path";

import { OrchestratorTreeDataProvider } from "./views.js";
import type { Snapshot } from "./types.js";
import type { OrchestratorClient } from "./orchestratorClient.js";

type EventSource = import("eventsource").default;

let eventSource: EventSource | null = null;
let dashboardPanel: vscode.WebviewPanel | null = null;
let reconnectTimer: NodeJS.Timeout | null = null;
let pollingFallback = false;
let reconnectAttempts = 0;
const MAX_RECONNECT_DELAY_MS = 30000;
const detailPanels = new Map<number, vscode.WebviewPanel>();
const consolePanels = new Map<number, vscode.WebviewPanel>();

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const isTest = process.env.IO_VSCODE_TEST === "1" || !!process.env.VSCODE_EXTENSION_TESTS;
  if (isTest) {
    registerTestCommands(context);
    return;
  }

  const output = vscode.window.createOutputChannel("Issue Orchestrator");
  const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBar.command = "issueOrchestrator.quickActions";
  statusBar.show();
  const diagnostics = vscode.languages.createDiagnosticCollection("Issue Orchestrator");
  let client: OrchestratorClient;
  const { McpClient } = await import("./mcpClient.js");
  client = new McpClient(context, output);
  try {
    await client.start();
  } catch (err) {
    output.appendLine(`Failed to start MCP client: ${String(err)}`);
    vscode.window.showErrorMessage("Issue Orchestrator MCP client failed to start. Check output for details.");
  }

  const provider = new OrchestratorTreeDataProvider(client, output, statusBar, (snapshot) => {
    updateDiagnostics(snapshot, diagnostics);
  });
  context.subscriptions.push(diagnostics);
  registerCommands(context, client, provider, output);

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider("issueOrchestrator.explorer", provider)
  );
  await provider.refresh();
  await connectEventStream(client, provider, output);
  await warnIfConfigMissing();

  context.subscriptions.push({
    dispose: () => {
      provider.stopPolling();
      statusBar.dispose();
      diagnostics.dispose();
      void client.stop();
      closeEventStream();
    },
  });
}

function registerTestCommands(context: vscode.ExtensionContext): void {
  const noop = async (): Promise<void> => {
    return;
  };
  const commands = [
    "issueOrchestrator.start",
    "issueOrchestrator.stop",
    "issueOrchestrator.pause",
    "issueOrchestrator.resume",
    "issueOrchestrator.refresh",
    "issueOrchestrator.openDashboard",
    "issueOrchestrator.quickActions",
    "issueOrchestrator.selectConfig",
    "issueOrchestrator.runDiagnostics",
    "issueOrchestrator.openDashboardExternal",
    "issueOrchestrator.openWorktree",
    "issueOrchestrator.openDetails",
    "issueOrchestrator.openSessionConsole",
    "issueOrchestrator.openPR",
    "issueOrchestrator.openLog",
    "issueOrchestrator.focusSession",
    "issueOrchestrator.killSession",
  ];
  for (const command of commands) {
    context.subscriptions.push(vscode.commands.registerCommand(command, noop));
  }
}

export async function deactivate(): Promise<void> {
  closeEventStream();
}

function registerCommands(
  context: vscode.ExtensionContext,
  client: OrchestratorClient,
  provider: OrchestratorTreeDataProvider,
  output: vscode.OutputChannel
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("issueOrchestrator.refresh", async () => {
      await runCommand(async () => provider.refresh(), output, "Refresh failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.start", async () => {
      await runCommand(async () => {
        await client.startOrchestrator();
        await provider.refresh();
      }, output, "Start failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.stop", async () => {
      const confirm = await vscode.window.showWarningMessage(
        "Stop the orchestrator process?",
        { modal: true },
        "Stop"
      );
      if (confirm === "Stop") {
        await runCommand(async () => {
          await client.stopOrchestrator();
          await provider.refresh();
        }, output, "Stop failed");
      }
    }),
    vscode.commands.registerCommand("issueOrchestrator.pause", async () => {
      await runCommand(async () => {
        await client.pause();
        await provider.refresh();
      }, output, "Pause failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.resume", async () => {
      await runCommand(async () => {
        await client.resume();
        await provider.refresh();
      }, output, "Resume failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.openDashboard", async () => {
      await runCommand(async () => {
        const urls = await client.getUrls();
        openDashboardWebview(urls.dashboard_url);
      }, output, "Open dashboard failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.openDashboardExternal", async () => {
      await runCommand(async () => {
        const urls = await client.getUrls();
        await vscode.env.openExternal(vscode.Uri.parse(urls.dashboard_url));
      }, output, "Open dashboard failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.quickActions", async () => {
      await runCommand(async () => {
        const options = [
          { label: "Start Orchestrator", command: "issueOrchestrator.start" },
          { label: "Stop Orchestrator", command: "issueOrchestrator.stop" },
          { label: "Pause Orchestrator", command: "issueOrchestrator.pause" },
          { label: "Resume Orchestrator", command: "issueOrchestrator.resume" },
          { label: "Refresh View", command: "issueOrchestrator.refresh" },
          { label: "Open Dashboard", command: "issueOrchestrator.openDashboard" },
          { label: "Open Dashboard in Browser", command: "issueOrchestrator.openDashboardExternal" },
          { label: "Select Config", command: "issueOrchestrator.selectConfig" },
          { label: "Run Diagnostics", command: "issueOrchestrator.runDiagnostics" },
        ];
        const selection = await vscode.window.showQuickPick(options, {
          placeHolder: "Issue Orchestrator Actions",
        }) as { label: string; command: string } | undefined;
        if (!selection) {
          return;
        }
        await vscode.commands.executeCommand(selection.command);
      }, output, "Quick actions failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.selectConfig", async () => {
      await runCommand(async () => {
        const selection = await vscode.window.showOpenDialog({
          canSelectMany: false,
          filters: { YAML: ["yaml", "yml"] },
        });
        if (!selection || selection.length === 0) {
          return;
        }
        const configPath = selection[0].fsPath;
        const repoRoot = inferRepoRoot(configPath);
        const config = vscode.workspace.getConfiguration("issueOrchestrator");
        await config.update("configPath", configPath, vscode.ConfigurationTarget.Workspace);
        if (repoRoot) {
          await config.update("repoRoot", repoRoot, vscode.ConfigurationTarget.Workspace);
        }
        vscode.window.showInformationMessage("Issue Orchestrator config updated.");
      }, output, "Select config failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.runDiagnostics", async () => {
      await runCommand(async () => {
        const report = await client.getDoctor();
        const content = buildDoctorMarkdown(report);
        const doc = await vscode.workspace.openTextDocument({ content, language: "markdown" });
        await vscode.window.showTextDocument(doc, { preview: false });
      }, output, "Diagnostics failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.openWorktree", async (item?: unknown) => {
      await runCommand(async () => {
        const issueNumber = await resolveIssueNumber(item);
        if (!issueNumber) {
          return;
        }
        const worktree = await client.getWorktree(issueNumber);
        const uri = vscode.Uri.file(worktree.worktree_path);
        await vscode.commands.executeCommand("vscode.openFolder", uri, true);
      }, output, "Open worktree failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.openDetails", async (item?: unknown) => {
      await runCommand(async () => {
        const issueNumber = await resolveIssueNumber(item);
        if (!issueNumber) {
          return;
        }
        await openDetailsPanel(issueNumber, provider, client);
      }, output, "Open details failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.openSessionConsole", async (item?: unknown) => {
      await runCommand(async () => {
        const issueNumber = await resolveIssueNumber(item);
        if (!issueNumber) {
          return;
        }
        await openSessionConsole(issueNumber, client, output);
      }, output, "Open session console failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.openPR", async (item?: unknown) => {
      await runCommand(async () => {
        const issueNumber = await resolveIssueNumber(item);
        if (!issueNumber) {
          return;
        }
        const snapshot = await getSnapshot(provider, client);
        const prUrl = findPrUrl(snapshot, issueNumber);
        if (!prUrl) {
          vscode.window.showWarningMessage(`No PR found for #${issueNumber}.`);
          return;
        }
        await vscode.env.openExternal(vscode.Uri.parse(prUrl));
      }, output, "Open PR failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.focusSession", async (item?: unknown) => {
      await runCommand(async () => {
        const issueNumber = await resolveIssueNumber(item);
        if (!issueNumber) {
          return;
        }
        await client.focusSession(issueNumber);
      }, output, "Focus session failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.killSession", async (item?: unknown) => {
      await runCommand(async () => {
        const issueNumber = await resolveIssueNumber(item);
        if (!issueNumber) {
          return;
        }
        const confirm = await vscode.window.showWarningMessage(
          `Kill session for #${issueNumber}?`,
          { modal: true },
          "Kill"
        );
        if (confirm !== "Kill") {
          return;
        }
        await client.killSession(issueNumber);
        await provider.refresh();
      }, output, "Kill session failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.openIssue", async (item?: unknown) => {
      await runCommand(async () => {
        const issueNumber = await resolveIssueNumber(item);
        if (!issueNumber) {
          return;
        }
        const repo = provider.getRepo();
        if (!repo) {
          vscode.window.showErrorMessage("Repo not available in orchestrator info.");
          return;
        }
        const url = `https://github.com/${repo}/issues/${issueNumber}`;
        await vscode.env.openExternal(vscode.Uri.parse(url));
      }, output, "Open issue failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.sendMessage", async (item?: unknown) => {
      await runCommand(async () => {
        const issueNumber = await resolveIssueNumber(item);
        if (!issueNumber) {
          return;
        }
        const text = await vscode.window.showInputBox({
          prompt: `Send message to issue #${issueNumber}`,
          placeHolder: "Type a message for the running agent session",
        });
        if (!text) {
          return;
        }
        await client.sendMessage(issueNumber, text);
        vscode.window.showInformationMessage(`Sent message to #${issueNumber}`);
      }, output, "Send message failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.openClaudeLog", async (item?: unknown) => {
      await runCommand(async () => {
        const issueNumber = await resolveIssueNumber(item);
        if (!issueNumber) {
          return;
        }
        const manifest = await client.getManifest(issueNumber);
        const manifestData = (manifest as { manifest?: { claude_log_path?: string } }).manifest;
        const logPath = manifestData?.claude_log_path;
        if (!logPath) {
          vscode.window.showErrorMessage("Claude log path not found in manifest.");
          return;
        }
        const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(logPath));
        await vscode.window.showTextDocument(doc, { preview: false });
      }, output, "Open Claude log failed");
    }),
    vscode.commands.registerCommand("issueOrchestrator.openOrchestratorLog", async (item?: unknown) => {
      await runCommand(async () => {
        const issueNumber = await resolveIssueNumber(item);
        if (!issueNumber) {
          return;
        }
        const logInfo = await client.getOrchestratorLog(issueNumber);
        const logPath = (logInfo as { filtered_log_path?: string }).filtered_log_path;
        if (!logPath) {
          vscode.window.showErrorMessage("Filtered orchestrator log not available.");
          return;
        }
        const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(logPath));
        await vscode.window.showTextDocument(doc, { preview: false });
      }, output, "Open orchestrator log failed");
    })
  );

  context.subscriptions.push(output);
}

async function resolveIssueNumber(item?: unknown): Promise<number | null> {
  if (item && typeof item === "object" && "issueNumber" in item) {
    const value = (item as { issueNumber: unknown }).issueNumber;
    if (typeof value === "number") {
      return value;
    }
  }

  const input = await vscode.window.showInputBox({
    prompt: "Issue number",
    placeHolder: "123",
    validateInput: (value) => (Number.isInteger(Number(value)) ? undefined : "Enter a number"),
  });

  if (!input) {
    return null;
  }

  return Number(input);
}

async function connectEventStream(
  client: OrchestratorClient,
  provider: OrchestratorTreeDataProvider,
  output: vscode.OutputChannel
): Promise<void> {
  try {
    const urls = await client.getUrls();
    const EventSource = (await import("eventsource")).default;
    closeEventStream();
    eventSource = new EventSource(urls.events_url);
    reconnectAttempts = 0;
    if (pollingFallback) {
      provider.stopPolling();
      pollingFallback = false;
    }

    const refreshOn = [
      "session.started",
      "session.completed",
      "session.failed",
      "session.blocked",
      "session.needs_human",
      "dependency.blocked",
      "dependency.unblocked",
      "queue.updated",
      "startup_complete",
    ];

    for (const eventName of refreshOn) {
    eventSource.addEventListener(eventName, (event) => {
        void provider.refresh();
        const detail = parseEventData(event);
        if (eventName === "session.completed" && detail.issue_number && shouldNotify("sessionCompleted")) {
          vscode.window.showInformationMessage(`Session completed for #${detail.issue_number}`);
        }
        if (eventName === "session.failed" && detail.issue_number && shouldNotify("sessionFailed")) {
          vscode.window.showWarningMessage(`Session failed for #${detail.issue_number}`);
        }
        if ((eventName === "session.blocked" || eventName === "session.needs_human") && detail.issue_number && shouldNotify("sessionBlocked")) {
          vscode.window.showWarningMessage(`Session blocked for #${detail.issue_number}`);
        }
      });
    }

    eventSource.onerror = (err: unknown) => {
      output.appendLine(`Event stream error: ${String(err)}`);
      scheduleReconnect(client, provider, output);
    };
  } catch (err) {
    output.appendLine(`Failed to connect event stream: ${String(err)}`);
    scheduleReconnect(client, provider, output);
  }
}

function parseEventData(event: { data?: string }): { [key: string]: unknown } {
  const raw = event.data as string | undefined;
  if (!raw) {
    return {};
  }
  try {
    return JSON.parse(raw) as { [key: string]: unknown };
  } catch {
    return {};
  }
}

function openDashboardWebview(url: string): void {
  if (dashboardPanel) {
    dashboardPanel.reveal();
    return;
  }

  dashboardPanel = vscode.window.createWebviewPanel(
    "issueOrchestratorDashboard",
    "Issue Orchestrator Dashboard",
    vscode.ViewColumn.Active,
    {
      enableScripts: true,
    }
  );

  dashboardPanel.webview.html = `<!DOCTYPE html>
  <html lang="en">
    <head>
      <meta charset="UTF-8" />
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; frame-src ${url}; style-src 'unsafe-inline';" />
      <style>
        html, body, iframe { height: 100%; width: 100%; margin: 0; padding: 0; }
        iframe { border: none; }
      </style>
    </head>
    <body>
      <iframe src="${url}"></iframe>
    </body>
  </html>`;

  dashboardPanel.onDidDispose(() => {
    dashboardPanel = null;
  });
}

function closeEventStream(): void {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
}

async function runCommand(action: () => Promise<void>, output: vscode.OutputChannel, message: string): Promise<void> {
  try {
    await action();
  } catch (err) {
    output.appendLine(`${message}: ${String(err)}`);
    vscode.window.showErrorMessage(message);
  }
}

function scheduleReconnect(
  client: OrchestratorClient,
  provider: OrchestratorTreeDataProvider,
  output: vscode.OutputChannel
): void {
  if (!pollingFallback) {
    provider.startPolling();
    pollingFallback = true;
  }
  if (reconnectTimer) {
    return;
  }
  const delay = Math.min(1000 * Math.pow(2, reconnectAttempts), MAX_RECONNECT_DELAY_MS);
  reconnectAttempts += 1;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    void connectEventStream(client, provider, output);
  }, delay);
}

async function openDetailsPanel(
  issueNumber: number,
  provider: OrchestratorTreeDataProvider,
  client: OrchestratorClient
): Promise<void> {
  const existing = detailPanels.get(issueNumber);
  if (existing) {
    existing.reveal();
    return;
  }

  const snapshot = await getSnapshot(provider, client);
  const detail = buildIssueDetail(snapshot, issueNumber);
  const phases = await client.getManifest(issueNumber).catch(() => null);
  const phaseInfo = await client.getPhases(issueNumber).catch(() => null);

  const panel = vscode.window.createWebviewPanel(
    "issueOrchestratorDetails",
    `Issue #${issueNumber} Details`,
    vscode.ViewColumn.Active,
    { enableScripts: true }
  );

  panel.webview.html = renderDetailsHtml(issueNumber, detail, phases, phaseInfo);
  panel.onDidDispose(() => {
    detailPanels.delete(issueNumber);
  });
  detailPanels.set(issueNumber, panel);
}

async function openSessionConsole(
  issueNumber: number,
  client: OrchestratorClient,
  output: vscode.OutputChannel
): Promise<void> {
  const existing = consolePanels.get(issueNumber);
  if (existing) {
    existing.reveal();
    return;
  }

  const panel = vscode.window.createWebviewPanel(
    "issueOrchestratorConsole",
    `Session Console #${issueNumber}`,
    vscode.ViewColumn.Active,
    { enableScripts: true }
  );

  const logData = await client.getClaudeLog(issueNumber, 200).catch((err) => {
    output.appendLine(`Failed to load Claude log for #${issueNumber}: ${String(err)}`);
    return null;
  });

  panel.webview.html = renderConsoleHtml(issueNumber, logData);

  panel.webview.onDidReceiveMessage(async (message) => {
    if (message?.type === "send" && typeof message.text === "string") {
      await client.sendMessage(issueNumber, message.text);
      const updated = await client.getClaudeLog(issueNumber, 200).catch(() => null);
      panel.webview.html = renderConsoleHtml(issueNumber, updated);
    }
    if (message?.type === "refresh") {
      const updated = await client.getClaudeLog(issueNumber, 200).catch(() => null);
      panel.webview.html = renderConsoleHtml(issueNumber, updated);
    }
  });

  panel.onDidDispose(() => {
    consolePanels.delete(issueNumber);
  });
  consolePanels.set(issueNumber, panel);
}

async function getSnapshot(provider: OrchestratorTreeDataProvider, client: OrchestratorClient) {
  return provider.getSnapshot() ?? (await client.getSnapshot());
}

function findPrUrl(snapshot: any, issueNumber: number): string | null {
  const pending = snapshot.status?.pending_reviews?.find((r: any) => r.issue_number === issueNumber);
  if (pending?.pr_url) {
    return pending.pr_url;
  }
  const history = snapshot.history?.history?.find((h: any) => h.issue_number === issueNumber);
  if (history?.pr_url) {
    return history.pr_url;
  }
  const job = snapshot.publish_jobs?.jobs?.find((j: any) => j.issue_number === issueNumber && j.pr_url);
  if (job?.pr_url) {
    return job.pr_url;
  }
  return null;
}

function buildIssueDetail(snapshot: any, issueNumber: number) {
  const active = snapshot.status?.active_sessions?.find((s: any) => s.issue_number === issueNumber);
  const queued = snapshot.status?.queue?.find((q: any) => q.number === issueNumber);
  const blocked = snapshot.blocked?.blocked?.find((b: any) => b.number === issueNumber);
  const history = snapshot.history?.history?.find((h: any) => h.issue_number === issueNumber);
  return { active, queued, blocked, history };
}

function renderDetailsHtml(issueNumber: number, detail: any, manifest: any, phases: any): string {
  const nonce = `${Date.now()}${Math.random()}`;
  const summary = detail.active ?? detail.queued ?? detail.blocked ?? detail.history ?? {};
  const title = summary.title ?? "Issue";
  const agent = summary.agent_type ?? summary.agent_label ?? "unknown";
  const status = detail.active ? "active" : detail.blocked ? "blocked" : detail.history ? detail.history.status : "queued";
  const prUrl = manifest?.manifest?.pr_url || summary.pr_url || "";
  const phaseList = phases?.phases ?? [];

  return `<!DOCTYPE html>
  <html lang="en">
    <head>
      <meta charset="UTF-8" />
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';" />
      <style>
        body { font-family: sans-serif; padding: 16px; }
        h1 { margin-top: 0; }
        .meta { display: grid; grid-template-columns: 160px 1fr; gap: 8px; margin-bottom: 16px; }
        code { background: #f3f3f3; padding: 2px 4px; border-radius: 4px; }
        .section { margin-top: 16px; }
        .pill { display: inline-block; padding: 2px 8px; border-radius: 12px; background: #e0e0e0; }
      </style>
    </head>
    <body>
      <h1>#${issueNumber} ${title}</h1>
      <div class="meta">
        <div>Status</div><div><span class="pill">${status}</span></div>
        <div>Agent</div><div>${agent ?? "unknown"}</div>
        <div>Runtime</div><div>${detail.active?.runtime_minutes ?? detail.history?.runtime_minutes ?? "n/a"}m</div>
        <div>Branch</div><div>${detail.active?.branch ?? "n/a"}</div>
        <div>PR</div><div>${prUrl ? `<a href="${prUrl}">${prUrl}</a>` : "n/a"}</div>
      </div>
      <div class="section">
        <h2>Phases</h2>
        <ul>
          ${phaseList.map((p: any) => `<li>${p.display_name} — ${p.status}</li>`).join("")}
        </ul>
      </div>
      <div class="section">
        <h2>Manifest</h2>
        <pre>${escapeHtml(JSON.stringify(manifest?.manifest ?? manifest ?? {}, null, 2))}</pre>
      </div>
    </body>
  </html>`;
}

function renderConsoleHtml(issueNumber: number, logData: any): string {
  const nonce = `${Date.now()}${Math.random()}`;
  const entries = Array.isArray(logData?.entries) ? logData.entries : [];
  const formatted = entries
    .map((entry: any) => {
      if (entry?._parse_error) {
        return `[parse error] ${entry._raw}`;
      }
      if (entry?.type && entry?.content) {
        return `${entry.type}: ${entry.content}`;
      }
      return JSON.stringify(entry);
    })
    .join("\\n");

  return `<!DOCTYPE html>
  <html lang="en">
    <head>
      <meta charset="UTF-8" />
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';" />
      <style>
        body { font-family: sans-serif; padding: 12px; }
        textarea { width: 100%; height: 80px; margin-top: 8px; }
        pre { background: #0b0b0b; color: #e8e8e8; padding: 12px; border-radius: 6px; height: 380px; overflow: auto; }
        button { margin-right: 8px; }
      </style>
    </head>
    <body>
      <h2>Session Console #${issueNumber}</h2>
      <pre>${escapeHtml(formatted || "No log entries found.")}</pre>
      <div>
        <textarea id="message" placeholder="Send a message to the agent..."></textarea>
      </div>
      <div>
        <button id="send">Send</button>
        <button id="refresh">Refresh</button>
      </div>
      <script nonce="${nonce}">
        const vscode = acquireVsCodeApi();
        document.getElementById("send").addEventListener("click", () => {
          const text = document.getElementById("message").value.trim();
          if (!text) return;
          vscode.postMessage({ type: "send", text });
        });
        document.getElementById("refresh").addEventListener("click", () => {
          vscode.postMessage({ type: "refresh" });
        });
      </script>
    </body>
  </html>`;
}

function escapeHtml(input: string): string {
  return input
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function updateDiagnostics(snapshot: any, diagnostics: vscode.DiagnosticCollection): void {
  const repoRoot = snapshot.info?.repo_root || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  if (!repoRoot) {
    return;
  }
  const uri = vscode.Uri.file(`${repoRoot}/.issue-orchestrator/diagnostics.txt`);
  const list: vscode.Diagnostic[] = [];

  const blocked = snapshot.blocked?.blocked ?? [];
  for (const issue of blocked) {
    const message = issue.blocked_summary || issue.flow_stage || "Issue blocked";
    list.push(new vscode.Diagnostic(new vscode.Range(0, 0, 0, 1), `#${issue.number}: ${message}`, vscode.DiagnosticSeverity.Warning));
  }

  const dependencyProblems = snapshot.dependency_problems?.problems ?? {};
  for (const [key, value] of Object.entries(dependencyProblems)) {
    const summary = (value as any).summary || "Dependency problem";
    list.push(new vscode.Diagnostic(new vscode.Range(0, 0, 0, 1), `#${key}: ${summary}`, vscode.DiagnosticSeverity.Warning));
  }

  diagnostics.set(uri, list);
}

function shouldNotify(kind: "sessionCompleted" | "sessionFailed" | "sessionBlocked"): boolean {
  const config = vscode.workspace.getConfiguration("issueOrchestrator");
  return config.get<boolean>(`notifications.${kind}`, true);
}

function inferRepoRoot(configPath: string): string | null {
  const parts = configPath.split(path.sep);
  const markerIndex = parts.lastIndexOf(".issue-orchestrator");
  if (markerIndex > 0) {
    return parts.slice(0, markerIndex).join(path.sep);
  }
  return null;
}

function buildDoctorMarkdown(report: Record<string, unknown>): string {
  const pretty = JSON.stringify(report, null, 2);
  return `# Issue Orchestrator Diagnostics\n\n\`\`\`json\n${pretty}\n\`\`\`\n`;
}

async function warnIfConfigMissing(): Promise<void> {
  const config = vscode.workspace.getConfiguration("issueOrchestrator");
  const configPath = config.get<string>("configPath");
  if (configPath && configPath.trim()) {
    return;
  }
  const repoRoot = config.get<string>("repoRoot") || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  if (!repoRoot) {
    return;
  }
  const expected = path.join(repoRoot, ".issue-orchestrator", "config", "default.yaml");
  try {
    await (vscode.workspace as typeof vscode.workspace & { fs: { stat(uri: vscode.Uri): Promise<void> } }).fs.stat(
      vscode.Uri.file(expected)
    );
  } catch {
    vscode.window.showInformationMessage(
      "Issue Orchestrator config not found. Use 'Issue Orchestrator: Select Config' to configure."
    );
  }
}
