// This file is generated from docs/api/ui-openapi.json.
// Do not edit by hand. Run: scripts/generate_ui_contracts.py



export interface AgentIdentityPayload {
  name: string;
  role: "coder" | "reviewer" | "rework" | "validator" | "e2e_runner" | "orchestrator";
}

export interface BlockedCodingAttemptPayload {
  agent: AgentIdentityPayload;
  blocked_at: string;
  commands: TimelineCommandPayload[];
  diagnostics: TimelineDiagnosticPayload[];
  issue_number: number;
  kind: "blocked_coding_attempt";
  reason: string;
  session_recording: SessionRecordingEvidencePayload;
  started_at?: string | null;
}

export interface BlockedIssuePayload {
  [key: string]: any;
}

export interface BlockedIssuesDialogPayload {
  blocked_issues: BlockedIssuePayload[];
  title: string;
}

export interface CodingOutputsPayload {
  pull_request_url?: string | null;
  worktree_path?: string | null;
}

export interface CompletedCodingAttemptPayload {
  agent: AgentIdentityPayload;
  commands: TimelineCommandPayload[];
  completed_at: string;
  completion_record: CompletionRecordEvidencePayload;
  issue_number: number;
  kind: "completed_coding_attempt";
  outputs: CodingOutputsPayload;
  session_recording: SessionRecordingEvidencePayload;
  started_at: string;
  validation: ValidationOutcomePayload;
}

export interface CompletionRecordEvidencePayload {
  kind: "available";
  path: string;
  summary?: string | null;
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

export interface DashboardIterationPayload {
  diagnostics: TimelineDiagnosticPayload[];
  issue_lifecycles: IssueLifecyclePayload[];
  kind: "dashboard_current";
  subject: TimelineSubjectPayload;
}

export interface DashboardTimelineContainerPayload {
  current: DashboardIterationPayload;
  kind: "dashboard";
  subject: TimelineSubjectPayload;
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

export interface E2EFailureDetailsAvailablePayload {
  kind: "available";
  longrepr: string;
}

export interface E2EFailureDetailsMissingPayload {
  diagnostics: TimelineDiagnosticPayload[];
  kind: "missing_evidence";
}

export interface E2EIssueAffordancePayload {
  branch_name?: string;
  issue_number: number;
  label?: string;
  run_id: number;
}

export interface E2ERunDetailPayload {
  actions: IssueDetailActionPayload[];
  artifacts: TestRunArtifactPayload[];
  blocked_detail: IssueDetailBlockedDetailPayload | null;
  cycles: E2ETimelineCyclePayload[];
  e2e_run_id?: number | null;
  events: E2ETimelineEventPayload[];
  issue_affordances: E2EIssueAffordancePayload[];
  issue_number: number | string;
  issue_url: string;
  lifecycle: LifecycleTimelineContainerPayload;
  phase_toc: E2ETimelinePhaseTocItemPayload[];
  previous_runs: Record<string, any>[];
  previous_runs_count: number;
  raw_events_count: number;
  reports: TestRunArtifactPayload[];
  results_by_category: E2ERunResultCategoriesPayload;
  results_summary: E2ERunResultsSummaryPayload;
  run: E2ERunExecutionPayload;
  run_count: number;
  runs: Record<string, any>[];
  status_explanation: string;
  summary: IssueDetailSummaryPayload;
  timeline_steps: Record<string, any>[];
  title: string;
  view?: string;
}

export interface E2ERunExecutionPayload {
  artifacts_dir: string | null;
  branch: string | null;
  command: string[];
  commit_sha: string | null;
  current_test: string | null;
  duration_seconds: number | null;
  exit_code: number | null;
  finished_at: string | null;
  id: number;
  log_path: string | null;
  orchestrator_id: string;
  pytest_args: string[];
  runner_kind: string;
  started_at: string;
  status: string;
  total_tests: number | null;
}

export interface E2ERunIterationPayload {
  diagnostics: TimelineDiagnosticPayload[];
  e2e_run: E2ERunLifecyclePayload;
  kind: "e2e_run";
  subject: TimelineSubjectPayload;
}

export interface E2ERunLifecyclePayload {
  completed_at?: string | null;
  diagnostics: TimelineDiagnosticPayload[];
  linked_issue_lifecycles: IssueLifecyclePayload[];
  run_id: number;
  started_at: string;
  tests: E2ETestExecutionPayload[];
}

export interface E2ERunResultCategoriesPayload {
  fixed: TestCaseResultPayload[];
  flaky: TestCaseResultPayload[];
  has_issue: TestCaseResultPayload[];
  passed: TestCaseResultPayload[];
  quarantined: TestCaseResultPayload[];
  skipped: TestCaseResultPayload[];
  untriaged: TestCaseResultPayload[];
}

export interface E2ERunResultsSummaryPayload {
  fixed: number;
  flaky: number;
  has_issue: number;
  passed: number;
  quarantined: number;
  skipped: number;
  total: number;
  untriaged: number;
}

export interface E2ERunTimelinePayload {
  cycles: E2ETimelineCyclePayload[];
  events: E2ETimelineEventPayload[];
  issue_affordances: E2EIssueAffordancePayload[];
  lifecycle: LifecycleTimelineContainerPayload;
  phase_toc: E2ETimelinePhaseTocItemPayload[];
}

export interface E2ESuiteTimelineContainerPayload {
  kind: "e2e_suite";
  runs: E2ERunIterationPayload[];
  subject: TimelineSubjectPayload;
}

export interface E2ETestOutputPayload {
  nodeid: string;
  source_path: string;
  system_err: string | null;
  system_out: string | null;
}

export interface E2ETimelineArtifactPayload {
  label: string;
  render_mode?: string | null;
  type: string;
  value: string;
}

export interface E2ETimelineCyclePayload {
  cycle: number;
  end: string | null;
  events: E2ETimelineEventPayload[];
  phases: string[];
  start: string | null;
  status: string;
  summary: string;
}

export interface E2ETimelineEventPayload {
  added?: string[];
  agent?: string;
  artifacts: E2ETimelineArtifactPayload[];
  attempt_index?: number;
  coder_response_text?: string;
  coder_response_type?: string;
  detail: string | null;
  duration_seconds?: number;
  event: string;
  event_id: string;
  event_intent: string;
  issue_affordances?: E2EIssueAffordancePayload[];
  issue_number: number;
  level: string;
  logical_cycle?: number;
  logical_phase?: string;
  logical_run?: number;
  longrepr?: string;
  narrative?: string;
  nodeid?: string;
  outcome?: string;
  parent_key: string;
  phase: string;
  removed?: string[];
  review_oriented: boolean;
  reviewer_agent?: string;
  reviewer_response_text?: string;
  reviewer_response_type?: string;
  rework_cycle?: number;
  role?: string;
  round_index?: number;
  rounds?: number;
  run_dir: string | null;
  run_id: string | null;
  source_event?: string;
  status: string;
  step: string;
  summary: string | null;
  task?: string;
  timeline_schema_version?: number;
  timestamp: string;
  unsupported_schema: boolean;
  views?: string[];
}

export interface E2ETimelinePhaseTocItemPayload {
  label: string;
  phase: string;
}

export interface FailedCodingAttemptPayload {
  agent?: AgentIdentityPayload | null;
  commands: TimelineCommandPayload[];
  diagnostics: TimelineDiagnosticPayload[];
  failed_at: string;
  issue_number: number;
  kind: "failed_coding_attempt";
  reason: string;
  session_recording: SessionRecordingEvidencePayload;
  started_at?: string | null;
}

export interface FailedE2ETestExecutionPayload {
  commands: TimelineCommandPayload[];
  completed_at: string;
  duration_seconds?: number | null;
  failure: E2EFailureEvidencePayload;
  kind: "failed_e2e_test";
  linked_issues: LinkedIssueLifecyclePayload[];
  nodeid: string;
  started_at: string;
}

export interface InfoDialogPayload {
  rows: DialogRowPayload[];
  title: string;
}

export interface IssueCyclePayload {
  coder: CodingAttemptPayload;
  cycle_number: number;
  diagnostics: TimelineDiagnosticPayload[];
  outcome: string;
  review: ReviewStagePayload;
}

export interface IssueDetailActionPayload {
  id: string;
  label: string;
  run_dir?: string | null;
  url?: string | null;
}

export interface IssueDetailBlockedDetailPayload {
  event_summary: string;
  labels: string[];
  reason: string;
  rework_info: string | null;
}

export interface IssueDetailPayload {
  actions: IssueDetailActionPayload[];
  blocked_detail: IssueDetailBlockedDetailPayload | null;
  cycles: Record<string, any>[];
  e2e_run_id?: number | null;
  events: Record<string, any>[];
  issue_number: number;
  issue_url: string;
  lifecycle?: LifecycleTimelineContainerPayload | null;
  phase_toc: Record<string, any>[];
  previous_runs: Record<string, any>[];
  previous_runs_count: number;
  raw_events_count: number;
  run_count: number;
  runs: Record<string, any>[];
  status_explanation: string;
  summary: IssueDetailSummaryPayload;
  timeline_steps: Record<string, any>[];
  title: string;
  view?: string;
}

export interface IssueDetailSummaryPayload {
  event_count: number;
  last_event: string;
  run_diagnostic?: IssueDetailValidationDiagnosticPayload | null;
  status: string;
  timeline_diagnostic?: IssueDetailTimelineDiagnosticPayload | null;
}

export interface IssueDetailTimelineDiagnosticPayload {
  dropped_missing_semantics: number;
  expected_timeline_store: string;
  expected_timeline_store_exists: boolean;
  resolved_run_dir: string | null;
  signals: string[];
  state: string;
}

export interface IssueDetailValidationDiagnosticPayload {
  command: string;
  exit_code: number;
  failed_tests: string[];
  failed_tests_preview: string[];
  junit_cases: TestCaseResultPayload[];
  reason: string;
  run_dir: string;
  session_name: string | null;
  state: string;
  suite: string;
  validation_record_path: string | null;
  validation_stderr: string | null;
  validation_stdout: string | null;
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

export interface IssueLifecyclePayload {
  cycles: IssueCyclePayload[];
  diagnostics: TimelineDiagnosticPayload[];
  issue_number: number;
  title: string;
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

export interface JUnitCasePayload {
  case_id: string;
  display_name: string;
  duration_seconds?: number | null;
  failure_details?: string | null;
  outcome: "passed" | "failed" | "error" | "skipped";
  suite_name?: string | null;
  system_err?: string | null;
  system_out?: string | null;
}

export interface LinkedIssueLifecyclePayload {
  command: OpenIssueTimelineCommandPayload;
  issue_number: number;
  relationship: "exercises" | "discovered" | "failed_with" | "validates";
}

export interface MissingCodingEvidencePayload {
  commands: TimelineCommandPayload[];
  diagnostics: TimelineDiagnosticPayload[];
  expected_state: "completed" | "running" | "blocked" | "failed";
  issue_number: number;
  kind: "missing_coding_evidence";
  missing: MissingEvidencePayload[];
  observed_at: string;
}

export interface MissingE2ETestEvidencePayload {
  commands: TimelineCommandPayload[];
  diagnostics: TimelineDiagnosticPayload[];
  kind: "missing_e2e_test_evidence";
  missing: MissingEvidencePayload[];
  nodeid: string;
  observed_at: string;
}

export interface MissingEvidencePayload {
  evidence: string;
  expected_ref?: string | null;
  kind: "missing_evidence";
  reason: string;
}

export interface MissingReviewEvidencePayload {
  commands: TimelineCommandPayload[];
  diagnostics: TimelineDiagnosticPayload[];
  expected_state: "approved" | "changes_requested" | "running";
  kind: "missing_review_evidence";
  missing: MissingEvidencePayload[];
  observed_at: string;
}

export interface OpenCompletionRecordCommandPayload {
  kind: "open_completion_record";
  label: string;
  path: string;
}

export interface OpenIssueTimelineCommandPayload {
  e2e_run_id?: number | null;
  issue_number: number;
  kind: "open_issue_timeline";
  label: string;
  scope_kind: "dashboard" | "e2e_run";
}

export interface OpenReviewFeedbackCommandPayload {
  event_ref?: string | null;
  issue_number: number;
  kind: "open_review_feedback";
  label: string;
}

export interface OpenSessionRecordingCommandPayload {
  issue_number: number;
  kind: "open_session_recording";
  label: string;
  round_index?: number | null;
  run_dir: string;
  session_role?: string | null;
}

export interface OpenValidationDetailsCommandPayload {
  issue_number: number;
  kind: "open_validation_details";
  label: string;
  run_dir: string;
}

export interface PassedE2ETestExecutionPayload {
  commands: TimelineCommandPayload[];
  completed_at: string;
  duration_seconds?: number | null;
  kind: "passed_e2e_test";
  linked_issues: LinkedIssueLifecyclePayload[];
  nodeid: string;
  started_at: string;
}

export interface PhaseDialogPayload {
  issue_number: number;
  phase: Record<string, any> | null;
  phases: Record<string, any>[];
  title: string;
}

export interface PublishFailedCodingAttemptPayload {
  agent: AgentIdentityPayload;
  commands: TimelineCommandPayload[];
  completed_at: string;
  completion_record: CompletionRecordEvidencePayload;
  diagnostics: TimelineDiagnosticPayload[];
  issue_number: number;
  kind: "publish_failed_coding_attempt";
  outputs: CodingOutputsPayload;
  publish_failed_at: string;
  reason: string;
  session_recording: SessionRecordingEvidencePayload;
  started_at: string;
  validation: ValidationOutcomePayload;
}

export interface ReviewApprovedPayload {
  commands: TimelineCommandPayload[];
  completed_at: string;
  kind: "review_approved";
  reviewer: AgentIdentityPayload;
  session_recording: SessionRecordingEvidencePayload;
  started_at: string;
  transcript: ReviewTranscriptEvidencePayload;
}

export interface ReviewChangesRequestedPayload {
  commands: TimelineCommandPayload[];
  completed_at: string;
  feedback_summary: string;
  kind: "review_changes_requested";
  reviewer: AgentIdentityPayload;
  session_recording: SessionRecordingEvidencePayload;
  started_at: string;
}

export interface ReviewFailedPayload {
  commands: TimelineCommandPayload[];
  diagnostics: TimelineDiagnosticPayload[];
  failed_at: string;
  kind: "review_failed";
  reason: string;
  reviewer?: AgentIdentityPayload | null;
  session_recording: SessionRecordingEvidencePayload;
  started_at?: string | null;
}

export interface ReviewNotReachedPayload {
  kind: "review_not_reached";
  reason: "coding_in_progress" | "coding_failed" | "publish_failed" | "validation_failed" | "not_required";
}

export interface ReviewRunningPayload {
  commands: TimelineCommandPayload[];
  kind: "review_running";
  reviewer: AgentIdentityPayload;
  session_recording: SessionRecordingEvidencePayload;
  started_at: string;
}

export interface ReviewSkippedPayload {
  kind: "review_skipped";
  reason: string;
}

export interface ReviewTranscriptAvailablePayload {
  kind: "available";
}

export interface ReviewTranscriptUnavailablePayload {
  diagnostics: TimelineDiagnosticPayload[];
  kind: "unavailable";
  reason: string;
}

export interface RunningCodingAttemptPayload {
  agent: AgentIdentityPayload;
  commands: TimelineCommandPayload[];
  issue_number: number;
  kind: "running_coding_attempt";
  session_recording: SessionRecordingEvidencePayload;
  started_at: string;
}

export interface RunningE2ETestExecutionPayload {
  commands: TimelineCommandPayload[];
  kind: "running_e2e_test";
  linked_issues: LinkedIssueLifecyclePayload[];
  nodeid: string;
  started_at: string;
}

export interface SessionDiagnosticsActionPayload {
  group?: "validation_artifacts" | "session_evidence" | "diagnostics" | null;
  issue_number?: number | null;
  label: string;
  path?: string | null;
  type: string;
  [key: string]: any;
}

export interface SessionDiagnosticsAnalysisPayload {
  detail?: string | null;
  headline: string;
  suggestions?: string[];
}

export interface SessionDiagnosticsDialogPayload {
  actions: SessionDiagnosticsActionPayload[];
  analysis?: SessionDiagnosticsAnalysisPayload | null;
  follow_up_issues?: SessionDiagnosticsFollowUpIssuePayload[];
  rows: DialogRowPayload[];
  title: string;
}

export interface SessionDiagnosticsFollowUpIssuePayload {
  blocking: boolean;
  evidence?: string | null;
  reason: string;
  suggested_labels?: string[];
  title: string;
}

export interface SessionRecordingAvailablePayload {
  command: OpenSessionRecordingCommandPayload;
  kind: "available";
  recording_path: string;
  run_dir: string;
}

export interface SessionRecordingUnavailablePayload {
  diagnostics: TimelineDiagnosticPayload[];
  kind: "unavailable";
  reason: string;
}

export interface ShowEventDetailsCommandPayload {
  event_ref: string;
  kind: "show_event_details";
  label: string;
}

export interface TestCaseHistoryPayload {
  outcome: string;
  run_id: number;
}

export interface TestCaseIssueLinkPayload {
  number: number;
  resolution: string | null;
  status: string;
}

export interface TestCaseResultPayload {
  case_id: string;
  category: string;
  display_name: string | null;
  duration_seconds: number | null;
  existing_issue: TestCaseIssueLinkPayload | null;
  failure_summary: string | null;
  flip_rate: number;
  flip_rate_percent: number;
  history: TestCaseHistoryPayload[];
  is_likely_flaky: boolean;
  is_quarantined: boolean;
  label: string;
  longrepr: string | null;
  nodeid: string;
  outcome: string;
  result_category: string;
  result_source: string;
  retry_outcome: string | null;
  suite_name: string | null;
  updated_at: string;
}

export interface TestRunArtifactPayload {
  kind: string;
  label: string;
  path: string;
}

export interface TimelineDiagnosticPayload {
  code: string;
  evidence_ref?: string | null;
  message: string;
  severity: "info" | "warning" | "error";
}

export interface TimelineSubjectPayload {
  id: string;
  kind: "dashboard" | "issue" | "e2e_suite" | "e2e_run";
  label: string;
  outcome?: string | null;
  status?: string | null;
}

export interface ValidationEvidenceMissingPayload {
  diagnostics: TimelineDiagnosticPayload[];
  expected_record_path?: string | null;
  kind: "missing_evidence";
}

export interface ValidationFailedPayload {
  command: string;
  details_command: OpenValidationDetailsCommandPayload;
  failure_summary: string;
  kind: "failed";
  record_path: string;
}

export interface ValidationFailureActionSectionPayload {
  actions: SessionDiagnosticsActionPayload[];
  title: string;
}

export interface ValidationFailureDialogPayload {
  action_sections: ValidationFailureActionSectionPayload[];
  command: string;
  ended_at: string;
  exit_code: number | null;
  failed_tests: string[];
  junit_cases: JUnitCasePayload[];
  reason: string;
  started_at: string;
  status: "passed" | "failed";
  stderr_excerpt: string[];
  stdout_excerpt: string[];
  suite: string;
  summary_rows: DialogRowPayload[];
  title: string;
}

export interface ValidationNotRunPayload {
  kind: "not_run";
  reason: "coding_in_progress" | "validation_disabled" | "not_required";
}

export interface ValidationPassedPayload {
  command: string;
  details_command: OpenValidationDetailsCommandPayload;
  kind: "passed";
  record_path: string;
}

export type CodingAttemptPayload = RunningCodingAttemptPayload | CompletedCodingAttemptPayload | PublishFailedCodingAttemptPayload | BlockedCodingAttemptPayload | FailedCodingAttemptPayload | MissingCodingEvidencePayload;

export type E2EFailureEvidencePayload = E2EFailureDetailsAvailablePayload | E2EFailureDetailsMissingPayload;

export type E2ETestExecutionPayload = PassedE2ETestExecutionPayload | FailedE2ETestExecutionPayload | RunningE2ETestExecutionPayload | MissingE2ETestEvidencePayload;

export type LifecycleTimelineContainerPayload = DashboardTimelineContainerPayload | E2ESuiteTimelineContainerPayload;

export type ReviewStagePayload = ReviewNotReachedPayload | ReviewSkippedPayload | ReviewRunningPayload | ReviewApprovedPayload | ReviewChangesRequestedPayload | ReviewFailedPayload | MissingReviewEvidencePayload;

export type ReviewTranscriptEvidencePayload = ReviewTranscriptAvailablePayload | ReviewTranscriptUnavailablePayload;

export type SessionRecordingEvidencePayload = SessionRecordingAvailablePayload | SessionRecordingUnavailablePayload;

export type TimelineCommandPayload = ShowEventDetailsCommandPayload | OpenCompletionRecordCommandPayload | OpenValidationDetailsCommandPayload | OpenSessionRecordingCommandPayload | OpenReviewFeedbackCommandPayload | OpenIssueTimelineCommandPayload;

export type ValidationOutcomePayload = ValidationPassedPayload | ValidationFailedPayload | ValidationNotRunPayload | ValidationEvidenceMissingPayload;
