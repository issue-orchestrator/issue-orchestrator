import type { McpStatusPayload, Snapshot } from "./types.js";

export interface OrchestratorClient {
  start(): Promise<void>;
  stop(): Promise<void>;
  getSnapshot(): Promise<Snapshot>;
  getStatus(): Promise<McpStatusPayload>;
  startOrchestrator(): Promise<void>;
  stopOrchestrator(force?: boolean): Promise<void>;
  pause(): Promise<void>;
  resume(): Promise<void>;
  refresh(): Promise<void>;
  getUrls(): Promise<{ base_url: string; dashboard_url: string; events_url: string; config_url: string }>;
  getDoctor(): Promise<Record<string, unknown>>;
  getWorktree(issueNumber: number): Promise<{ worktree_path: string; session_name?: string | null }>;
  getManifest(issueNumber: number): Promise<Record<string, unknown>>;
  getPhases(issueNumber: number): Promise<Record<string, unknown>>;
  getClaudeLog(issueNumber: number, limit?: number): Promise<Record<string, unknown>>;
  getOrchestratorLog(issueNumber: number): Promise<Record<string, unknown>>;
  focusSession(issueNumber: number): Promise<void>;
  killSession(issueNumber: number): Promise<void>;
  sendMessage(issueNumber: number, text: string): Promise<void>;
}
