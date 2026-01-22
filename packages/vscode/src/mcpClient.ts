import * as vscode from "vscode";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

import type { OrchestratorClient } from "./orchestratorClient.js";
import type {
  McpStatusPayload,
  Snapshot,
} from "./types.js";

export class McpClient implements OrchestratorClient {
  private client: Client | null = null;
  private transport: StdioClientTransport | null = null;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly output: vscode.OutputChannel
  ) {}

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

    this.transport = new StdioClientTransport({
      command,
      args,
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

  async startOrchestrator(): Promise<void> {
    await this.callTool("orchestrator.start");
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

  async getDoctor(): Promise<Record<string, unknown>> {
    return this.callTool("orchestrator.doctor");
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

  async sendMessage(issueNumber: number, text: string): Promise<void> {
    await this.callTool("orchestrator.session.send", { issue_number: issueNumber, text });
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
