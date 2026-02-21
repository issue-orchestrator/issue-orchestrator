// This file is generated from docs/api/ui-openapi.json.
// Do not edit by hand. Run: scripts/generate_ui_contracts.py



export interface BlockedIssuePayload {
  [key: string]: any;
}

export interface BlockedIssuesDialogPayload {
  blocked_issues: BlockedIssuePayload[];
  title: string;
}

export interface ConfigDialogPayload {
  config_text: string;
  title: string;
}

export interface DashboardDataPayload {
  agents: string[];
  e2eLastRun?: Record<string, any> | null;
  e2eRunning: boolean;
  githubOwner: string;
  githubRepo: string;
  paused: boolean;
  queueRefreshSeconds: number;
  repo: string;
  repoRoot: string;
  startupComplete: boolean;
  [key: string]: any;
}

export interface DashboardViewModelPayload {
  active_count: number;
  active_items: IssueItemPayload[];
  active_session_count: number;
  active_tab: string;
  agents: string[];
  awaiting_merge_count: number;
  awaiting_merge_items: IssueItemPayload[];
  blocked_count: number;
  blocked_items: IssueItemPayload[];
  completed_count: number;
  completed_items: IssueItemPayload[];
  dashboard_data: DashboardDataPayload;
  e2e_count: number;
  e2e_items: IssueItemPayload[];
  e2e_page: number;
  e2e_status: Record<string, any>;
  e2e_total: number;
  e2e_total_pages: number;
  flow_columns: Record<string, any>[];
  github_owner: string;
  github_repo: string;
  history_items: IssueItemPayload[];
  issues: IssueItemPayload[];
  paused: boolean;
  provider_outages: Record<string, any>[];
  queue_count: number;
  queue_items: IssueItemPayload[];
  queue_page: number;
  queue_refresh_seconds: number;
  queue_total: number;
  queue_total_pages: number;
  repo: string;
  repo_root: string;
  scope_summary: Record<string, any>;
  shutdown_requested: boolean;
  startup_message: string;
  startup_status: string;
}

export interface DebugDialogPayload {
  sections: DialogSectionPayload[];
  title: string;
}

export interface DialogRowPayload {
  label: string;
  value: string;
}

export interface DialogSectionPayload {
  rows: DialogRowPayload[];
  title: string;
}

export interface DoctorCheckPayload {
  detail?: string | null;
  name?: string | null;
  status?: string | null;
}

export interface DoctorDialogPayload {
  checks: DoctorCheckPayload[];
  overall: string;
  title: string;
}

export interface InfoDialogPayload {
  rows: DialogRowPayload[];
  title: string;
}

export interface IssueDetailPayload {
  actions: Record<string, any>[];
  blocked_detail: Record<string, any> | null;
  cycles: Record<string, any>[];
  events: Record<string, any>[];
  issue_number: number;
  issue_url: string;
  journey_cycles: Record<string, any>[];
  journey_steps: Record<string, any>[];
  lifecycle_count: number;
  phase_toc: Record<string, any>[];
  previous_cycles: Record<string, any>[];
  previous_cycles_count: number;
  raw_events_count: number;
  status_explanation: string;
  summary: Record<string, any>;
  title: string;
}

export interface IssueItemPayload {
  action?: string | null;
  action_hint?: string | null;
  issue_number?: number | string | null;
  issue_url?: string | null;
  status?: string | null;
  title?: string | null;
  url?: string | null;
  [key: string]: any;
}

export interface IssueRowPayload {
  html: string;
  issue_number?: number | string | null;
}

export interface IssueRowsPayload {
  active_tab: string;
  count: number;
  rows: IssueRowPayload[];
}

export interface PhaseDialogPayload {
  issue_number: number;
  phase: Record<string, any> | null;
  phases: Record<string, any>[];
  title: string;
}

export interface SessionDiagnosticsActionPayload {
  issue_number?: number | null;
  label: string;
  path?: string | null;
  type: string;
  [key: string]: any;
}

export interface SessionDiagnosticsDialogPayload {
  actions: SessionDiagnosticsActionPayload[];
  rows: DialogRowPayload[];
  title: string;
}
