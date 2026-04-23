import * as vscode from "vscode";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

import type { OrchestratorClient } from "./orchestratorClient.js";
import type {
  McpStatusPayload,
  Snapshot,
  StartResponse,
  DoctorReport,
} from "./types.js";

export class McpClient implements OrchestratorClient {
  private client: Client | null = null;
  private transport: StdioClientTransport | null = null;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly output: vscode.OutputChannel
  ) {}

  isConnected(): boolean {
    return this.client !== null;
  }

  async start(): Promise<void> {
    if (this.client) {
      return;
    }

    const config = vscode.workspace.getConfiguration("issueOrchestrator");
    const repoRoot = this.resolveRepoRoot(config.get<string>("repoRoot"));
    const configPath = config.get<string>("configPath") || "";
    const instanceId = config.get<string>("instanceId") || "";
    const autoStart = config.get<boolean>("autoStart", true);
    const command = config.get<string>("mcpCommand", "issue-orchestrator-mcp");

    const args: string[] = [];
    if (repoRoot) {
      args.push("--repo-root", repoRoot);
    }
    if (configPath) {
      args.push("--config", configPath);
    }
    if (instanceId) {
      args.push("--instance-id", instanceId);
    }
    if (autoStart) {
      args.push("--auto-start");
    }

    // The MCP SDK's StdioClientTransport inherits only a minimal env
    // by default. Two things need to reach the MCP subprocess:
    //
    // 1. The orchestrator's Control API bearer token (security #5987
    //    F3). Without it every authenticated call would 401.
    // 2. E2E harness variables (``IO_E2E_*`` / ``IO_VSCODE_E2E``) so
    //    tests can steer the subprocess at a specific port.
    //
    // Plus the POSIX basics any subprocess needs to boot
    // (``PATH``, ``HOME``, locale). The previous version forwarded
    // the entire VS Code process env, which also leaks unrelated
    // secrets (AWS keys, GitHub tokens, arbitrary third-party
    // credentials the operator set in their shell) to whatever
    // binary the ``issueOrchestrator.mcpCommand`` setting points at
    // — and that command is user-configurable. Restrict to an
    // explicit allowlist (security #6017 re-review P5).
    const MCP_ENV_ALLOWLIST: readonly string[] = [
      "PATH",
      "HOME",
      "USER",
      "LOGNAME",
      "SHELL",
      "LANG",
      "TZ",
      "TMPDIR",
      "TEMP",
      "TMP",
      "XDG_CACHE_HOME",
      "XDG_CONFIG_HOME",
      "XDG_DATA_HOME",
      "XDG_RUNTIME_DIR",
      "ISSUE_ORCHESTRATOR_API_TOKEN",
      "ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN",
      "ISSUE_ORCHESTRATOR_PYTHON",
      "ISSUE_ORCHESTRATOR_CC_SNAPSHOT",
      "ISSUE_ORCHESTRATOR_REPO_ROOT",
      "PYTHONPATH",
      "IO_VSCODE_E2E",
    ];
    const env: Record<string, string> = {};
    for (const name of MCP_ENV_ALLOWLIST) {
      const value = process.env[name];
      if (value !== undefined) {
        env[name] = value;
      }
    }
    for (const [name, value] of Object.entries(process.env)) {
      if (value === undefined) continue;
      if (name.startsWith("IO_E2E_") || /^LC_/.test(name)) {
        env[name] = value;
      }
    }
    this.transport = new StdioClientTransport({
      command,
      args,
      env,
    });

    this.client = new Client(
      { name: "issue-orchestrator-vscode", version: "0.1.0" },
      { capabilities: {} }
    );

    await this.client.connect(this.transport);
    this.output.appendLine("MCP client connected.");
  }

  async stop(): Promise<void> {
    if (this.client) {
      await this.client.close();
      this.client = null;
    }
    if (this.transport) {
      await this.transport.close();
      this.transport = null;
    }
  }

  async getSnapshot(): Promise<Snapshot> {
    return this.callTool<Snapshot>("orchestrator.snapshot");
  }

  async getStatus(): Promise<McpStatusPayload> {
    return this.callTool<McpStatusPayload>("orchestrator.status");
  }

  async startOrchestrator(): Promise<StartResponse> {
    return this.callTool<StartResponse>("orchestrator.start");
  }

  async stopOrchestrator(force = false): Promise<void> {
    await this.callTool("orchestrator.stop", { force });
  }

  async pause(): Promise<void> {
    await this.callTool("orchestrator.pause");
  }

  async resume(): Promise<void> {
    await this.callTool("orchestrator.resume");
  }

  async refresh(): Promise<void> {
    await this.callTool("orchestrator.refresh");
  }

  async getUrls(): Promise<{ base_url: string; dashboard_url: string; events_url: string; config_url: string }> {
    return this.callTool("orchestrator.urls");
  }

  async getDoctor(): Promise<DoctorReport> {
    return this.callTool<DoctorReport>("orchestrator.doctor");
  }

  async getWorktree(issueNumber: number): Promise<{ worktree_path: string; session_name?: string | null }> {
    return this.callTool("orchestrator.session.worktree", { issue_number: issueNumber });
  }

  async getManifest(issueNumber: number): Promise<Record<string, unknown>> {
    return this.callTool("orchestrator.session.manifest", { issue_number: issueNumber });
  }

  async getPhases(issueNumber: number): Promise<Record<string, unknown>> {
    return this.callTool("orchestrator.session.phases", { issue_number: issueNumber });
  }

  async getClaudeLog(issueNumber: number, limit = 200): Promise<Record<string, unknown>> {
    return this.callTool("orchestrator.session.claude_log", { issue_number: issueNumber, limit });
  }

  async getOrchestratorLog(issueNumber: number): Promise<Record<string, unknown>> {
    return this.callTool("orchestrator.session.orchestrator_log", { issue_number: issueNumber });
  }

  async focusSession(issueNumber: number): Promise<void> {
    await this.callTool("orchestrator.session.focus", { issue_number: issueNumber });
  }

  async killSession(issueNumber: number): Promise<void> {
    await this.callTool("orchestrator.session.kill", { issue_number: issueNumber });
  }

  private async callTool<T = Record<string, unknown>>(name: string, args: Record<string, unknown> = {}): Promise<T> {
    if (!this.client) {
      throw new Error("MCP client not connected");
    }

    const result = await this.client.callTool({ name, arguments: args });
    if (result.isError) {
      throw new Error(`MCP tool error: ${name}`);
    }

    const content = (result as { content?: unknown[] }).content ?? [];
    if (!Array.isArray(content) || content.length === 0) {
      return {} as T;
    }

    const payload = content[0] as {
      type?: string;
      text?: string;
      json?: unknown;
    };

    if (payload.type === "json" && payload.json !== undefined) {
      return payload.json as T;
    }

    if (payload.text) {
      try {
        return JSON.parse(payload.text) as T;
      } catch (err) {
        this.output.appendLine(`Failed to parse MCP text payload for ${name}: ${String(err)}`);
      }
    }

    return payload as T;
  }

  private resolveRepoRoot(configured: string | undefined): string | undefined {
    if (configured && configured.trim()) {
      return configured.trim();
    }
    const folders = vscode.workspace.workspaceFolders;
    if (!folders || folders.length === 0) {
      return undefined;
    }
    return folders[0].uri.fsPath;
  }
}
