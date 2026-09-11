// This file is generated from docs/api/ui-openapi.json.
// Do not edit by hand. Run: scripts/generate_ui_contracts.py
//
// Browser runtime validators for UI JSON payloads (issue #6337).
//
// ``SCHEMAS`` is the validation-relevant projection of the canonical
// OpenAPI components; the engine below enforces it in the browser so the
// contract layer reaches all the way into JavaScript instead of stopping
// at the server response model.
//
// This module owns JSON *payload shape* only.  Contextual invariants a
// schema cannot know (does this command's run_id match the DOM row that
// owns it?) stay in the UI owner abstractions that hold that context.
//
// Parse JSON through ``ui_contract_json.js`` rather than calling
// ``validate`` directly; that helper is the shared fail-closed entry
// point for every browser JSON boundary.
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.uiContractValidators = api;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    const SCHEMAS = {
        "ActiveSessionSummaryPayload": {
            "additionalProperties": false,
            "properties": {
                "agent_type": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "branch": {
                    "type": "string"
                },
                "issue_number": {
                    "type": "integer"
                },
                "runtime_minutes": {
                    "type": "integer"
                },
                "status": {
                    "enum": [
                        "running",
                        "slow"
                    ]
                },
                "title": {
                    "type": "string"
                },
                "worktree_path": {
                    "type": "string"
                }
            },
            "required": [
                "issue_number",
                "title",
                "runtime_minutes",
                "agent_type",
                "status",
                "branch",
                "worktree_path"
            ],
            "type": "object"
        },
        "AgentIdentityPayload": {
            "additionalProperties": false,
            "properties": {
                "name": {
                    "type": "string"
                },
                "role": {
                    "enum": [
                        "coder",
                        "reviewer",
                        "rework",
                        "validator",
                        "e2e_runner",
                        "orchestrator"
                    ]
                }
            },
            "required": [
                "name",
                "role"
            ],
            "type": "object"
        },
        "AttemptPayload": {
            "additionalProperties": false,
            "properties": {
                "attempt_key": {
                    "type": "string"
                },
                "attempt_label": {
                    "type": "string"
                },
                "attempt_number": {
                    "type": "integer"
                },
                "cycles": {
                    "items": {
                        "$ref": "#/components/schemas/IssueCyclePayload"
                    },
                    "type": "array"
                },
                "expanded": {
                    "type": "boolean"
                },
                "outcome": {
                    "$ref": "#/components/schemas/OutcomeBadgePayload"
                },
                "reset_from_scratch": {
                    "type": "boolean"
                },
                "run_id": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "session_run_ids": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "time_label": {
                    "type": "string"
                },
                "timestamp": {
                    "type": "string"
                }
            },
            "required": [
                "attempt_number",
                "attempt_label",
                "outcome",
                "attempt_key",
                "run_id",
                "session_run_ids",
                "timestamp",
                "time_label",
                "expanded",
                "reset_from_scratch",
                "cycles"
            ],
            "type": "object"
        },
        "BlockedCodingAttemptPayload": {
            "additionalProperties": false,
            "properties": {
                "agent": {
                    "$ref": "#/components/schemas/AgentIdentityPayload"
                },
                "blocked_at": {
                    "type": "string"
                },
                "commands": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineCommandPayload"
                    },
                    "type": "array"
                },
                "diagnostics": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineDiagnosticPayload"
                    },
                    "type": "array"
                },
                "issue_number": {
                    "type": "integer"
                },
                "kind": {
                    "const": "blocked_coding_attempt"
                },
                "reason": {
                    "type": "string"
                },
                "session_recording": {
                    "$ref": "#/components/schemas/SessionRecordingEvidencePayload"
                },
                "started_at": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "kind",
                "issue_number",
                "agent",
                "blocked_at",
                "reason",
                "session_recording",
                "diagnostics",
                "commands"
            ],
            "type": "object"
        },
        "BlockedIssuePayload": {
            "additionalProperties": false,
            "properties": {
                "agent_type": {
                    "type": "string"
                },
                "all_blocking_labels": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "blocking_label": {
                    "type": "string"
                },
                "failure_reason": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "has_completion": {
                    "type": "boolean"
                },
                "issue_number": {
                    "type": "integer"
                },
                "issue_url": {
                    "type": "string"
                },
                "needs_human": {
                    "type": "boolean"
                },
                "run_dir": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "title": {
                    "type": "string"
                },
                "worktree_path": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "issue_number",
                "title",
                "agent_type",
                "blocking_label",
                "all_blocking_labels",
                "needs_human",
                "failure_reason",
                "issue_url",
                "worktree_path",
                "run_dir",
                "has_completion"
            ],
            "type": "object"
        },
        "BlockedIssuesDialogPayload": {
            "additionalProperties": false,
            "properties": {
                "blocked_issues": {
                    "items": {
                        "$ref": "#/components/schemas/BlockedIssuePayload"
                    },
                    "type": "array"
                },
                "title": {
                    "type": "string"
                }
            },
            "required": [
                "title",
                "blocked_issues"
            ],
            "type": "object"
        },
        "BlockedIssuesPayload": {
            "additionalProperties": false,
            "properties": {
                "blocked_issues": {
                    "items": {
                        "$ref": "#/components/schemas/BlockedIssuePayload"
                    },
                    "type": "array"
                }
            },
            "required": [
                "blocked_issues"
            ],
            "type": "object"
        },
        "CapturedOutputAvailabilityPayload": {
            "additionalProperties": false,
            "properties": {
                "stderr_available": {
                    "type": "boolean"
                },
                "stdout_available": {
                    "type": "boolean"
                }
            },
            "required": [
                "stdout_available",
                "stderr_available"
            ],
            "type": "object"
        },
        "ClientCapabilitiesPayload": {
            "additionalProperties": false,
            "properties": {
                "focus_session": {
                    "type": "boolean"
                },
                "host_platform": {
                    "type": "string"
                },
                "local_server_paths_only": {
                    "type": "boolean"
                },
                "open_path": {
                    "type": "boolean"
                },
                "reveal_worktree": {
                    "type": "boolean"
                }
            },
            "required": [
                "focus_session",
                "open_path",
                "reveal_worktree",
                "local_server_paths_only",
                "host_platform"
            ],
            "type": "object"
        },
        "CodingAttemptPayload": {
            "discriminator": {
                "propertyName": "kind"
            },
            "oneOf": [
                {
                    "$ref": "#/components/schemas/RunningCodingAttemptPayload"
                },
                {
                    "$ref": "#/components/schemas/CompletedCodingAttemptPayload"
                },
                {
                    "$ref": "#/components/schemas/PublishFailedCodingAttemptPayload"
                },
                {
                    "$ref": "#/components/schemas/BlockedCodingAttemptPayload"
                },
                {
                    "$ref": "#/components/schemas/FailedCodingAttemptPayload"
                },
                {
                    "$ref": "#/components/schemas/MissingCodingEvidencePayload"
                }
            ]
        },
        "CodingOutputsPayload": {
            "additionalProperties": false,
            "properties": {
                "pull_request_url": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "worktree_path": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [],
            "type": "object"
        },
        "CompletedCodingAttemptPayload": {
            "additionalProperties": false,
            "properties": {
                "agent": {
                    "$ref": "#/components/schemas/AgentIdentityPayload"
                },
                "commands": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineCommandPayload"
                    },
                    "type": "array"
                },
                "completed_at": {
                    "type": "string"
                },
                "completion_record": {
                    "$ref": "#/components/schemas/CompletionRecordEvidencePayload"
                },
                "issue_number": {
                    "type": "integer"
                },
                "kind": {
                    "const": "completed_coding_attempt"
                },
                "outputs": {
                    "$ref": "#/components/schemas/CodingOutputsPayload"
                },
                "session_recording": {
                    "$ref": "#/components/schemas/SessionRecordingEvidencePayload"
                },
                "started_at": {
                    "type": "string"
                },
                "validation": {
                    "$ref": "#/components/schemas/ValidationOutcomePayload"
                }
            },
            "required": [
                "kind",
                "issue_number",
                "agent",
                "started_at",
                "completed_at",
                "completion_record",
                "validation",
                "session_recording",
                "outputs",
                "commands"
            ],
            "type": "object"
        },
        "CompletionIntakeReceiptPayload": {
            "additionalProperties": false,
            "properties": {
                "content_sha256": {
                    "pattern": "^[0-9a-f]{64}$",
                    "type": "string"
                },
                "entry_id": {
                    "pattern": "^[0-9a-f]{64}$",
                    "type": "string"
                }
            },
            "required": [
                "entry_id",
                "content_sha256"
            ],
            "type": "object"
        },
        "CompletionRecordEvidencePayload": {
            "additionalProperties": false,
            "properties": {
                "kind": {
                    "const": "available"
                },
                "path": {
                    "type": "string"
                },
                "summary": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "kind",
                "path"
            ],
            "type": "object"
        },
        "CompletionResumeOutcomePayload": {
            "additionalProperties": false,
            "properties": {
                "actions_taken": {
                    "items": {
                        "type": "string"
                    },
                    "type": [
                        "array",
                        "null"
                    ]
                },
                "errors": {
                    "items": {
                        "type": "string"
                    },
                    "type": [
                        "array",
                        "null"
                    ]
                },
                "message": {
                    "type": "string"
                },
                "pr_url": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "success": {
                    "type": "boolean"
                }
            },
            "required": [
                "success",
                "message",
                "pr_url",
                "actions_taken",
                "errors"
            ],
            "type": "object"
        },
        "CompletionSubmissionPayload": {
            "additionalProperties": false,
            "properties": {
                "content_sha256": {
                    "pattern": "^[0-9a-f]{64}$",
                    "type": "string"
                },
                "raw_bytes": {
                    "maxLength": 2796204,
                    "type": "string"
                },
                "submission_key": {
                    "maxLength": 128,
                    "minLength": 1,
                    "type": "string"
                }
            },
            "required": [
                "raw_bytes",
                "content_sha256",
                "submission_key"
            ],
            "type": "object"
        },
        "ConfigDialogPayload": {
            "additionalProperties": false,
            "properties": {
                "config_text": {
                    "type": "string"
                },
                "title": {
                    "type": "string"
                }
            },
            "required": [
                "title",
                "config_text"
            ],
            "type": "object"
        },
        "ControlCenterRecoveryRowsPayload": {
            "discriminator": {
                "propertyName": "status"
            },
            "oneOf": [
                {
                    "$ref": "#/components/schemas/RecoveryAvailablePayload"
                },
                {
                    "$ref": "#/components/schemas/RecoveryEmptyPayload"
                },
                {
                    "$ref": "#/components/schemas/RecoveryUnavailablePayload"
                }
            ]
        },
        "CopySessionRecordingCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "issue_number": {
                    "type": "integer"
                },
                "kind": {
                    "const": "copy_session_recording"
                },
                "label": {
                    "type": "string"
                },
                "run_dir": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "label",
                "issue_number",
                "run_dir"
            ],
            "type": "object"
        },
        "CreateE2EUntriagedIssuesCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "kind": {
                    "const": "create_e2e_untriaged_issues"
                },
                "label": {
                    "type": "string"
                },
                "run_id": {
                    "minimum": 1,
                    "type": "integer"
                }
            },
            "required": [
                "kind",
                "label",
                "run_id"
            ],
            "type": "object"
        },
        "CycleArtifactsPayload": {
            "additionalProperties": false,
            "properties": {
                "has_review_feedback": {
                    "type": "boolean"
                },
                "log_url": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "pr_number": {
                    "type": [
                        "integer",
                        "null"
                    ]
                },
                "pr_url": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "review_decision": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/CycleReviewArtifactPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "review_report": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/CycleReviewArtifactPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "run_dir": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "log_url",
                "pr_url",
                "pr_number",
                "has_review_feedback",
                "run_dir",
                "review_report",
                "review_decision"
            ],
            "type": "object"
        },
        "CycleReviewArtifactPayload": {
            "additionalProperties": false,
            "properties": {
                "artifact_path": {
                    "type": "string"
                },
                "artifact_type": {
                    "enum": [
                        "review_report",
                        "review_decision"
                    ]
                },
                "label": {
                    "type": "string"
                },
                "render_mode": {
                    "enum": [
                        "markdown",
                        "json"
                    ]
                },
                "run_dir": {
                    "type": "string"
                }
            },
            "required": [
                "artifact_type",
                "label",
                "artifact_path",
                "render_mode",
                "run_dir"
            ],
            "type": "object"
        },
        "CycleValidationBadgePayload": {
            "additionalProperties": false,
            "properties": {
                "command": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/OpenValidationDetailsCommandPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "state": {
                    "enum": [
                        "pending",
                        "not_validated",
                        "passed",
                        "failed"
                    ]
                }
            },
            "required": [
                "state",
                "command"
            ],
            "type": "object"
        },
        "DashboardDataPayload": {
            "additionalProperties": true,
            "properties": {
                "agents": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "configMode": {
                    "type": "string"
                },
                "configName": {
                    "type": "string"
                },
                "e2eLastRun": {
                    "additionalProperties": true,
                    "type": [
                        "object",
                        "null"
                    ]
                },
                "e2eRunning": {
                    "type": "boolean"
                },
                "githubOwner": {
                    "type": "string"
                },
                "githubRepo": {
                    "type": "string"
                },
                "paused": {
                    "type": "boolean"
                },
                "providerCircuit": {
                    "$ref": "#/components/schemas/ProviderCircuitStatusPayload"
                },
                "queueRefreshSeconds": {
                    "type": "integer"
                },
                "repo": {
                    "type": "string"
                },
                "repoRoot": {
                    "type": "string"
                },
                "startupComplete": {
                    "type": "boolean"
                },
                "techLeadActivity": {
                    "$ref": "#/components/schemas/TechLeadActivityPayload"
                },
                "validationConfigured": {
                    "type": "boolean"
                }
            },
            "required": [
                "startupComplete",
                "paused",
                "e2eRunning",
                "queueRefreshSeconds",
                "repo",
                "repoRoot",
                "configName",
                "configMode",
                "githubOwner",
                "githubRepo",
                "agents",
                "validationConfigured",
                "providerCircuit",
                "techLeadActivity"
            ],
            "type": "object"
        },
        "DashboardIterationPayload": {
            "additionalProperties": false,
            "properties": {
                "diagnostics": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineDiagnosticPayload"
                    },
                    "type": "array"
                },
                "issue_lifecycles": {
                    "items": {
                        "$ref": "#/components/schemas/IssueLifecyclePayload"
                    },
                    "type": "array"
                },
                "kind": {
                    "const": "dashboard_current"
                },
                "subject": {
                    "$ref": "#/components/schemas/TimelineSubjectPayload"
                }
            },
            "required": [
                "kind",
                "subject",
                "issue_lifecycles",
                "diagnostics"
            ],
            "type": "object"
        },
        "DashboardTimelineContainerPayload": {
            "additionalProperties": false,
            "properties": {
                "current": {
                    "$ref": "#/components/schemas/DashboardIterationPayload"
                },
                "kind": {
                    "const": "dashboard"
                },
                "subject": {
                    "$ref": "#/components/schemas/TimelineSubjectPayload"
                }
            },
            "required": [
                "kind",
                "subject",
                "current"
            ],
            "type": "object"
        },
        "DashboardViewModelPayload": {
            "additionalProperties": false,
            "properties": {
                "active_count": {
                    "type": "integer"
                },
                "active_items": {
                    "items": {
                        "$ref": "#/components/schemas/IssueItemPayload"
                    },
                    "type": "array"
                },
                "active_session_count": {
                    "type": "integer"
                },
                "active_tab": {
                    "type": "string"
                },
                "agents": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "awaiting_merge_count": {
                    "type": "integer"
                },
                "awaiting_merge_items": {
                    "items": {
                        "$ref": "#/components/schemas/IssueItemPayload"
                    },
                    "type": "array"
                },
                "blocked_count": {
                    "type": "integer"
                },
                "blocked_items": {
                    "items": {
                        "$ref": "#/components/schemas/IssueItemPayload"
                    },
                    "type": "array"
                },
                "completed_count": {
                    "type": "integer"
                },
                "completed_items": {
                    "items": {
                        "$ref": "#/components/schemas/IssueItemPayload"
                    },
                    "type": "array"
                },
                "dashboard_data": {
                    "$ref": "#/components/schemas/DashboardDataPayload"
                },
                "e2e_count": {
                    "type": "integer"
                },
                "e2e_items": {
                    "items": {
                        "$ref": "#/components/schemas/IssueItemPayload"
                    },
                    "type": "array"
                },
                "e2e_page": {
                    "type": "integer"
                },
                "e2e_status": {
                    "additionalProperties": true,
                    "type": "object"
                },
                "e2e_total": {
                    "type": "integer"
                },
                "e2e_total_pages": {
                    "type": "integer"
                },
                "flow_columns": {
                    "items": {
                        "$ref": "#/components/schemas/FlowColumnPayload"
                    },
                    "type": "array"
                },
                "github_owner": {
                    "type": "string"
                },
                "github_repo": {
                    "type": "string"
                },
                "history_items": {
                    "items": {
                        "$ref": "#/components/schemas/IssueItemPayload"
                    },
                    "type": "array"
                },
                "issues": {
                    "items": {
                        "$ref": "#/components/schemas/IssueItemPayload"
                    },
                    "type": "array"
                },
                "paused": {
                    "type": "boolean"
                },
                "queue_count": {
                    "type": "integer"
                },
                "queue_items": {
                    "items": {
                        "$ref": "#/components/schemas/IssueItemPayload"
                    },
                    "type": "array"
                },
                "queue_page": {
                    "type": "integer"
                },
                "queue_refresh_seconds": {
                    "type": "integer"
                },
                "queue_total": {
                    "type": "integer"
                },
                "queue_total_pages": {
                    "type": "integer"
                },
                "recent_e2e_runs": {
                    "$ref": "#/components/schemas/RecentE2ERunsPayload"
                },
                "repo": {
                    "type": "string"
                },
                "repo_root": {
                    "type": "string"
                },
                "scope_summary": {
                    "additionalProperties": true,
                    "type": "object"
                },
                "shutdown_requested": {
                    "type": "boolean"
                },
                "startup_message": {
                    "type": "string"
                },
                "startup_status": {
                    "type": "string"
                }
            },
            "required": [
                "issues",
                "active_items",
                "queue_items",
                "blocked_items",
                "history_items",
                "e2e_items",
                "completed_items",
                "awaiting_merge_items",
                "flow_columns",
                "scope_summary",
                "active_count",
                "queue_count",
                "blocked_count",
                "e2e_count",
                "completed_count",
                "awaiting_merge_count",
                "active_tab",
                "paused",
                "shutdown_requested",
                "active_session_count",
                "startup_status",
                "startup_message",
                "repo",
                "repo_root",
                "github_owner",
                "github_repo",
                "queue_page",
                "queue_total_pages",
                "queue_total",
                "queue_refresh_seconds",
                "agents",
                "e2e_status",
                "e2e_page",
                "e2e_total_pages",
                "e2e_total",
                "recent_e2e_runs",
                "dashboard_data"
            ],
            "type": "object"
        },
        "DebugAgentPayload": {
            "additionalProperties": false,
            "properties": {
                "command": {
                    "type": "string"
                },
                "timeout": {
                    "type": "integer"
                }
            },
            "required": [
                "timeout",
                "command"
            ],
            "type": "object"
        },
        "DebugDialogPayload": {
            "additionalProperties": false,
            "properties": {
                "sections": {
                    "items": {
                        "$ref": "#/components/schemas/DialogSectionPayload"
                    },
                    "type": "array"
                },
                "title": {
                    "type": "string"
                }
            },
            "required": [
                "title",
                "sections"
            ],
            "type": "object"
        },
        "DebugFilteringPayload": {
            "additionalProperties": false,
            "properties": {
                "label": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "milestone": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "milestones": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                }
            },
            "required": [
                "label",
                "milestone",
                "milestones"
            ],
            "type": "object"
        },
        "DebugSnapshotPayload": {
            "additionalProperties": false,
            "properties": {
                "agents": {
                    "additionalProperties": {
                        "$ref": "#/components/schemas/DebugAgentPayload"
                    },
                    "type": "object"
                },
                "config_path": {
                    "type": "string"
                },
                "paused": {
                    "type": "boolean"
                },
                "priority_queue": {
                    "items": {
                        "type": "integer"
                    },
                    "type": "array"
                },
                "repo_root": {
                    "type": "string"
                },
                "startup_options": {
                    "$ref": "#/components/schemas/DebugStartupOptionsPayload"
                }
            },
            "required": [
                "paused",
                "config_path",
                "repo_root",
                "priority_queue",
                "agents",
                "startup_options"
            ],
            "type": "object"
        },
        "DebugStartupOptionsPayload": {
            "additionalProperties": false,
            "properties": {
                "filtering": {
                    "$ref": "#/components/schemas/DebugFilteringPayload"
                },
                "max_sessions": {
                    "type": "integer"
                },
                "test_mode": {
                    "type": "boolean"
                },
                "ui_mode": {
                    "type": "string"
                },
                "web_port": {
                    "type": "integer"
                }
            },
            "required": [
                "ui_mode",
                "web_port",
                "test_mode",
                "filtering",
                "max_sessions"
            ],
            "type": "object"
        },
        "DependencyProblemPayload": {
            "additionalProperties": false,
            "properties": {
                "issue_number": {
                    "type": "integer"
                },
                "issue_title": {
                    "type": "string"
                },
                "issue_url": {
                    "type": "string"
                },
                "summary": {
                    "type": "string"
                }
            },
            "required": [
                "issue_number",
                "issue_title",
                "summary",
                "issue_url"
            ],
            "type": "object"
        },
        "DependencyProblemsPayload": {
            "additionalProperties": false,
            "properties": {
                "problems": {
                    "additionalProperties": {
                        "$ref": "#/components/schemas/DependencyProblemPayload"
                    },
                    "type": "object"
                }
            },
            "required": [
                "problems"
            ],
            "type": "object"
        },
        "DialogActionCommandPayload": {
            "discriminator": {
                "propertyName": "kind"
            },
            "oneOf": [
                {
                    "$ref": "#/components/schemas/OpenPathCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/OpenSessionRecordingCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/CopySessionRecordingCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/ViewClaudeLogCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/OpenOrchestratorLogCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/OpenSessionDiagnosticsCommandPayload"
                }
            ]
        },
        "DialogActionPayload": {
            "additionalProperties": false,
            "properties": {
                "command": {
                    "$ref": "#/components/schemas/DialogActionCommandPayload"
                },
                "group": {
                    "enum": [
                        "validation_artifacts",
                        "session_evidence",
                        "diagnostics"
                    ]
                }
            },
            "required": [
                "command",
                "group"
            ],
            "type": "object"
        },
        "DialogRowPayload": {
            "additionalProperties": false,
            "properties": {
                "label": {
                    "type": "string"
                },
                "value": {
                    "type": "string"
                },
                "value_kind": {
                    "enum": [
                        "timestamp"
                    ],
                    "type": "string"
                }
            },
            "required": [
                "label",
                "value"
            ],
            "type": "object"
        },
        "DialogSectionPayload": {
            "additionalProperties": false,
            "properties": {
                "rows": {
                    "items": {
                        "$ref": "#/components/schemas/DialogRowPayload"
                    },
                    "type": "array"
                },
                "title": {
                    "type": "string"
                }
            },
            "required": [
                "title",
                "rows"
            ],
            "type": "object"
        },
        "DoctorCheckPayload": {
            "additionalProperties": false,
            "properties": {
                "detail": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "name": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "status": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "type": "object"
        },
        "DoctorDialogPayload": {
            "additionalProperties": false,
            "properties": {
                "checks": {
                    "items": {
                        "$ref": "#/components/schemas/DoctorCheckPayload"
                    },
                    "type": "array"
                },
                "overall": {
                    "type": "string"
                },
                "title": {
                    "type": "string"
                }
            },
            "required": [
                "title",
                "overall",
                "checks"
            ],
            "type": "object"
        },
        "DoctorReportCheckPayload": {
            "additionalProperties": false,
            "properties": {
                "detail": {
                    "type": "string"
                },
                "expandable": {
                    "additionalProperties": true,
                    "type": [
                        "object",
                        "null"
                    ]
                },
                "name": {
                    "type": "string"
                },
                "status": {
                    "$ref": "#/components/schemas/HealthStatus"
                }
            },
            "required": [
                "name",
                "status",
                "detail"
            ],
            "type": "object"
        },
        "DoctorReportPayload": {
            "additionalProperties": false,
            "properties": {
                "checks": {
                    "items": {
                        "$ref": "#/components/schemas/DoctorReportCheckPayload"
                    },
                    "type": "array"
                },
                "overall": {
                    "$ref": "#/components/schemas/HealthStatus"
                }
            },
            "required": [
                "overall",
                "checks"
            ],
            "type": "object"
        },
        "E2EArtifactDiagnosticPayload": {
            "additionalProperties": false,
            "properties": {
                "collected_count": {
                    "type": "integer"
                },
                "configured_glob_count": {
                    "type": "integer"
                },
                "state": {
                    "enum": [
                        "collected",
                        "globs_matched_nothing",
                        "not_configured"
                    ],
                    "type": "string"
                }
            },
            "required": [
                "state",
                "collected_count",
                "configured_glob_count"
            ],
            "type": "object"
        },
        "E2EFailureDetailsAvailablePayload": {
            "additionalProperties": false,
            "properties": {
                "kind": {
                    "const": "available"
                },
                "longrepr": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "longrepr"
            ],
            "type": "object"
        },
        "E2EFailureDetailsMissingPayload": {
            "additionalProperties": false,
            "properties": {
                "diagnostics": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineDiagnosticPayload"
                    },
                    "type": "array"
                },
                "kind": {
                    "const": "missing_evidence"
                }
            },
            "required": [
                "kind",
                "diagnostics"
            ],
            "type": "object"
        },
        "E2EFailureEvidencePayload": {
            "discriminator": {
                "propertyName": "kind"
            },
            "oneOf": [
                {
                    "$ref": "#/components/schemas/E2EFailureDetailsAvailablePayload"
                },
                {
                    "$ref": "#/components/schemas/E2EFailureDetailsMissingPayload"
                }
            ]
        },
        "E2EIssueAffordancePayload": {
            "additionalProperties": false,
            "properties": {
                "branch_name": {
                    "type": "string"
                },
                "issue_number": {
                    "type": "integer"
                },
                "label": {
                    "type": "string"
                },
                "run_id": {
                    "type": "integer"
                }
            },
            "required": [
                "issue_number",
                "run_id"
            ],
            "type": "object"
        },
        "E2ERunDetailPayload": {
            "additionalProperties": false,
            "properties": {
                "actions": {
                    "items": {
                        "$ref": "#/components/schemas/IssueDetailActionPayload"
                    },
                    "type": "array"
                },
                "artifact_diagnostic": {
                    "$ref": "#/components/schemas/E2EArtifactDiagnosticPayload"
                },
                "artifacts": {
                    "items": {
                        "$ref": "#/components/schemas/TestRunArtifactPayload"
                    },
                    "type": "array"
                },
                "attempt_count": {
                    "type": "integer"
                },
                "attempts": {
                    "items": {
                        "$ref": "#/components/schemas/AttemptPayload"
                    },
                    "type": "array"
                },
                "blocked_detail": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/IssueDetailBlockedDetailPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "cycles": {
                    "items": {
                        "$ref": "#/components/schemas/E2ETimelineCyclePayload"
                    },
                    "type": "array"
                },
                "e2e_run_id": {
                    "type": [
                        "integer",
                        "null"
                    ]
                },
                "events": {
                    "items": {
                        "$ref": "#/components/schemas/E2ETimelineEventPayload"
                    },
                    "type": "array"
                },
                "issue_affordances": {
                    "items": {
                        "$ref": "#/components/schemas/E2EIssueAffordancePayload"
                    },
                    "type": "array"
                },
                "issue_number": {
                    "type": [
                        "integer",
                        "string"
                    ]
                },
                "issue_url": {
                    "type": "string"
                },
                "lifecycle": {
                    "$ref": "#/components/schemas/LifecycleTimelineContainerPayload"
                },
                "phase_toc": {
                    "items": {
                        "$ref": "#/components/schemas/E2ETimelinePhaseTocItemPayload"
                    },
                    "type": "array"
                },
                "previous_runs": {
                    "items": {
                        "additionalProperties": true,
                        "type": "object"
                    },
                    "type": "array"
                },
                "previous_runs_count": {
                    "type": "integer"
                },
                "raw_events_count": {
                    "type": "integer"
                },
                "reports": {
                    "items": {
                        "$ref": "#/components/schemas/TestRunArtifactPayload"
                    },
                    "type": "array"
                },
                "results_by_category": {
                    "$ref": "#/components/schemas/E2ERunResultCategoriesPayload"
                },
                "results_summary": {
                    "$ref": "#/components/schemas/E2ERunResultsSummaryPayload"
                },
                "run": {
                    "$ref": "#/components/schemas/E2ERunExecutionPayload"
                },
                "status_explanation": {
                    "type": "string"
                },
                "summary": {
                    "$ref": "#/components/schemas/IssueDetailSummaryPayload"
                },
                "timeline_steps": {
                    "items": {
                        "additionalProperties": true,
                        "type": "object"
                    },
                    "type": "array"
                },
                "title": {
                    "type": "string"
                },
                "view": {
                    "$ref": "#/components/schemas/TimelineView"
                }
            },
            "required": [
                "run",
                "results_summary",
                "results_by_category",
                "artifacts",
                "reports",
                "artifact_diagnostic",
                "issue_number",
                "title",
                "issue_url",
                "phase_toc",
                "cycles",
                "events",
                "summary",
                "actions",
                "status_explanation",
                "attempts",
                "timeline_steps",
                "attempt_count",
                "previous_runs",
                "previous_runs_count",
                "raw_events_count",
                "blocked_detail",
                "issue_affordances",
                "lifecycle"
            ],
            "type": "object"
        },
        "E2ERunExecutionPayload": {
            "additionalProperties": false,
            "properties": {
                "artifacts_dir": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "branch": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "command": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "commit_sha": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "current_test": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "duration_seconds": {
                    "type": [
                        "number",
                        "null"
                    ]
                },
                "exit_code": {
                    "type": [
                        "integer",
                        "null"
                    ]
                },
                "finished_at": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "id": {
                    "type": "integer"
                },
                "log_excerpt": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "log_path": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "orchestrator_id": {
                    "type": "string"
                },
                "pytest_args": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "runner_kind": {
                    "type": "string"
                },
                "started_at": {
                    "type": "string"
                },
                "status": {
                    "type": "string"
                },
                "total_tests": {
                    "type": [
                        "integer",
                        "null"
                    ]
                }
            },
            "required": [
                "id",
                "orchestrator_id",
                "started_at",
                "finished_at",
                "status",
                "exit_code",
                "duration_seconds",
                "pytest_args",
                "command",
                "runner_kind",
                "commit_sha",
                "branch",
                "log_path",
                "log_excerpt",
                "artifacts_dir",
                "total_tests",
                "current_test"
            ],
            "type": "object"
        },
        "E2ERunIterationPayload": {
            "additionalProperties": false,
            "properties": {
                "diagnostics": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineDiagnosticPayload"
                    },
                    "type": "array"
                },
                "e2e_run": {
                    "$ref": "#/components/schemas/E2ERunLifecyclePayload"
                },
                "kind": {
                    "const": "e2e_run"
                },
                "subject": {
                    "$ref": "#/components/schemas/TimelineSubjectPayload"
                }
            },
            "required": [
                "kind",
                "subject",
                "e2e_run",
                "diagnostics"
            ],
            "type": "object"
        },
        "E2ERunLifecyclePayload": {
            "additionalProperties": false,
            "properties": {
                "completed_at": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "diagnostics": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineDiagnosticPayload"
                    },
                    "type": "array"
                },
                "linked_issue_lifecycles": {
                    "items": {
                        "$ref": "#/components/schemas/IssueLifecyclePayload"
                    },
                    "type": "array"
                },
                "run_id": {
                    "type": "integer"
                },
                "started_at": {
                    "type": "string"
                },
                "tests": {
                    "items": {
                        "$ref": "#/components/schemas/E2ETestExecutionPayload"
                    },
                    "type": "array"
                }
            },
            "required": [
                "run_id",
                "started_at",
                "tests",
                "linked_issue_lifecycles",
                "diagnostics"
            ],
            "type": "object"
        },
        "E2ERunResultCategoriesPayload": {
            "additionalProperties": false,
            "properties": {
                "fixed": {
                    "items": {
                        "$ref": "#/components/schemas/TestCaseResultPayload"
                    },
                    "type": "array"
                },
                "flaky": {
                    "items": {
                        "$ref": "#/components/schemas/TestCaseResultPayload"
                    },
                    "type": "array"
                },
                "has_issue": {
                    "items": {
                        "$ref": "#/components/schemas/TestCaseResultPayload"
                    },
                    "type": "array"
                },
                "passed": {
                    "items": {
                        "$ref": "#/components/schemas/TestCaseResultPayload"
                    },
                    "type": "array"
                },
                "quarantined": {
                    "items": {
                        "$ref": "#/components/schemas/TestCaseResultPayload"
                    },
                    "type": "array"
                },
                "skipped": {
                    "items": {
                        "$ref": "#/components/schemas/TestCaseResultPayload"
                    },
                    "type": "array"
                },
                "untriaged": {
                    "items": {
                        "$ref": "#/components/schemas/TestCaseResultPayload"
                    },
                    "type": "array"
                }
            },
            "required": [
                "untriaged",
                "has_issue",
                "flaky",
                "fixed",
                "passed",
                "quarantined",
                "skipped"
            ],
            "type": "object"
        },
        "E2ERunResultCountsPayload": {
            "additionalProperties": false,
            "properties": {
                "errored": {
                    "minimum": 0,
                    "type": "integer"
                },
                "failed": {
                    "minimum": 0,
                    "type": "integer"
                },
                "passed": {
                    "minimum": 0,
                    "type": "integer"
                },
                "quarantined": {
                    "minimum": 0,
                    "type": "integer"
                },
                "skipped": {
                    "minimum": 0,
                    "type": "integer"
                },
                "total": {
                    "minimum": 0,
                    "type": "integer"
                }
            },
            "required": [
                "passed",
                "failed",
                "errored",
                "skipped",
                "quarantined",
                "total"
            ],
            "type": "object"
        },
        "E2ERunResultsSummaryPayload": {
            "additionalProperties": false,
            "properties": {
                "fixed": {
                    "type": "integer"
                },
                "flaky": {
                    "type": "integer"
                },
                "has_issue": {
                    "type": "integer"
                },
                "passed": {
                    "type": "integer"
                },
                "quarantined": {
                    "type": "integer"
                },
                "skipped": {
                    "type": "integer"
                },
                "total": {
                    "type": "integer"
                },
                "untriaged": {
                    "type": "integer"
                }
            },
            "required": [
                "untriaged",
                "has_issue",
                "flaky",
                "fixed",
                "passed",
                "quarantined",
                "skipped",
                "total"
            ],
            "type": "object"
        },
        "E2ERunTimelinePayload": {
            "additionalProperties": false,
            "properties": {
                "cycles": {
                    "items": {
                        "$ref": "#/components/schemas/E2ETimelineCyclePayload"
                    },
                    "type": "array"
                },
                "events": {
                    "items": {
                        "$ref": "#/components/schemas/E2ETimelineEventPayload"
                    },
                    "type": "array"
                },
                "issue_affordances": {
                    "items": {
                        "$ref": "#/components/schemas/E2EIssueAffordancePayload"
                    },
                    "type": "array"
                },
                "lifecycle": {
                    "$ref": "#/components/schemas/LifecycleTimelineContainerPayload"
                },
                "phase_toc": {
                    "items": {
                        "$ref": "#/components/schemas/E2ETimelinePhaseTocItemPayload"
                    },
                    "type": "array"
                }
            },
            "required": [
                "events",
                "phase_toc",
                "cycles",
                "issue_affordances",
                "lifecycle"
            ],
            "type": "object"
        },
        "E2ESuiteTimelineContainerPayload": {
            "additionalProperties": false,
            "properties": {
                "kind": {
                    "const": "e2e_suite"
                },
                "runs": {
                    "items": {
                        "$ref": "#/components/schemas/E2ERunIterationPayload"
                    },
                    "type": "array"
                },
                "subject": {
                    "$ref": "#/components/schemas/TimelineSubjectPayload"
                }
            },
            "required": [
                "kind",
                "subject",
                "runs"
            ],
            "type": "object"
        },
        "E2ETestExecutionPayload": {
            "discriminator": {
                "propertyName": "kind"
            },
            "oneOf": [
                {
                    "$ref": "#/components/schemas/PassedE2ETestExecutionPayload"
                },
                {
                    "$ref": "#/components/schemas/FailedE2ETestExecutionPayload"
                },
                {
                    "$ref": "#/components/schemas/RunningE2ETestExecutionPayload"
                },
                {
                    "$ref": "#/components/schemas/MissingE2ETestEvidencePayload"
                }
            ]
        },
        "E2ETestOutputPayload": {
            "additionalProperties": false,
            "properties": {
                "nodeid": {
                    "type": "string"
                },
                "source_path": {
                    "type": "string"
                },
                "system_err": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "system_out": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "nodeid",
                "system_out",
                "system_err",
                "source_path"
            ],
            "type": "object"
        },
        "E2ETimelineArtifactPayload": {
            "additionalProperties": false,
            "properties": {
                "label": {
                    "type": "string"
                },
                "render_mode": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "type": {
                    "type": "string"
                },
                "value": {
                    "type": "string"
                }
            },
            "required": [
                "type",
                "label",
                "value"
            ],
            "type": "object"
        },
        "E2ETimelineCyclePayload": {
            "additionalProperties": false,
            "properties": {
                "cycle": {
                    "type": "integer"
                },
                "end": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "events": {
                    "items": {
                        "$ref": "#/components/schemas/E2ETimelineEventPayload"
                    },
                    "type": "array"
                },
                "phases": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "start": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "status": {
                    "type": "string"
                },
                "summary": {
                    "type": "string"
                }
            },
            "required": [
                "cycle",
                "start",
                "end",
                "status",
                "phases",
                "events",
                "summary"
            ],
            "type": "object"
        },
        "E2ETimelineEventPayload": {
            "additionalProperties": false,
            "properties": {
                "added": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "agent": {
                    "type": "string"
                },
                "artifacts": {
                    "items": {
                        "$ref": "#/components/schemas/E2ETimelineArtifactPayload"
                    },
                    "type": "array"
                },
                "attempt_index": {
                    "type": "integer"
                },
                "coder_response_text": {
                    "type": "string"
                },
                "coder_response_type": {
                    "type": "string"
                },
                "detail": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "duration_seconds": {
                    "type": "number"
                },
                "event": {
                    "type": "string"
                },
                "event_id": {
                    "type": "string"
                },
                "event_intent": {
                    "type": "string"
                },
                "issue_affordances": {
                    "items": {
                        "$ref": "#/components/schemas/E2EIssueAffordancePayload"
                    },
                    "type": "array"
                },
                "issue_number": {
                    "type": "integer"
                },
                "level": {
                    "type": "string"
                },
                "logical_cycle": {
                    "type": "integer"
                },
                "logical_phase": {
                    "type": "string"
                },
                "logical_run": {
                    "type": "integer"
                },
                "longrepr": {
                    "type": "string"
                },
                "narrative": {
                    "type": "string"
                },
                "nodeid": {
                    "type": "string"
                },
                "outcome": {
                    "type": "string"
                },
                "parent_key": {
                    "type": "string"
                },
                "phase": {
                    "type": "string"
                },
                "removed": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "response_type": {
                    "type": "string"
                },
                "review_oriented": {
                    "type": "boolean"
                },
                "reviewer_agent": {
                    "type": "string"
                },
                "reviewer_response_text": {
                    "type": "string"
                },
                "reviewer_response_type": {
                    "type": "string"
                },
                "rework_cycle": {
                    "type": "integer"
                },
                "role": {
                    "type": "string"
                },
                "round_index": {
                    "type": "integer"
                },
                "rounds": {
                    "type": "integer"
                },
                "run_dir": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "run_id": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "source_event": {
                    "type": "string"
                },
                "status": {
                    "type": "string"
                },
                "step": {
                    "type": "string"
                },
                "summary": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "task": {
                    "type": "string"
                },
                "timeline_schema_version": {
                    "type": "integer"
                },
                "timestamp": {
                    "type": "string"
                },
                "unsupported_schema": {
                    "type": "boolean"
                },
                "views": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                }
            },
            "required": [
                "event_id",
                "timestamp",
                "event",
                "issue_number",
                "phase",
                "step",
                "status",
                "level",
                "summary",
                "parent_key",
                "detail",
                "run_id",
                "run_dir",
                "artifacts",
                "unsupported_schema",
                "review_oriented",
                "event_intent"
            ],
            "type": "object"
        },
        "E2ETimelinePhaseTocItemPayload": {
            "additionalProperties": false,
            "properties": {
                "label": {
                    "type": "string"
                },
                "phase": {
                    "type": "string"
                }
            },
            "required": [
                "phase",
                "label"
            ],
            "type": "object"
        },
        "ExcludedIssuePayload": {
            "additionalProperties": false,
            "properties": {
                "agent_type": {
                    "type": "string"
                },
                "blocked_summary": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "excluded_reason": {
                    "type": "string"
                },
                "flow_stage": {
                    "enum": [
                        "not_eligible"
                    ]
                },
                "flow_steps": {
                    "items": {
                        "$ref": "#/components/schemas/FlowStepPayload"
                    },
                    "type": "array"
                },
                "issue_number": {
                    "type": "integer"
                },
                "issue_url": {
                    "type": "string"
                },
                "title": {
                    "type": "string"
                }
            },
            "required": [
                "issue_number",
                "title",
                "agent_type",
                "issue_url",
                "excluded_reason",
                "flow_stage",
                "flow_steps",
                "blocked_summary"
            ],
            "type": "object"
        },
        "ExcludedIssuesPayload": {
            "additionalProperties": false,
            "properties": {
                "excluded": {
                    "items": {
                        "$ref": "#/components/schemas/ExcludedIssuePayload"
                    },
                    "type": "array"
                }
            },
            "required": [
                "excluded"
            ],
            "type": "object"
        },
        "ExpandE2ERunCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "kind": {
                    "const": "expand_e2e_run"
                },
                "label": {
                    "type": "string"
                },
                "run_id": {
                    "minimum": 1,
                    "type": "integer"
                }
            },
            "required": [
                "kind",
                "label",
                "run_id"
            ],
            "type": "object"
        },
        "FailedCodingAttemptPayload": {
            "additionalProperties": false,
            "properties": {
                "agent": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/AgentIdentityPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "commands": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineCommandPayload"
                    },
                    "type": "array"
                },
                "diagnostics": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineDiagnosticPayload"
                    },
                    "type": "array"
                },
                "failed_at": {
                    "type": "string"
                },
                "issue_number": {
                    "type": "integer"
                },
                "kind": {
                    "const": "failed_coding_attempt"
                },
                "reason": {
                    "type": "string"
                },
                "session_recording": {
                    "$ref": "#/components/schemas/SessionRecordingEvidencePayload"
                },
                "started_at": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "kind",
                "issue_number",
                "failed_at",
                "reason",
                "session_recording",
                "diagnostics",
                "commands"
            ],
            "type": "object"
        },
        "FailedE2ETestExecutionPayload": {
            "additionalProperties": false,
            "properties": {
                "commands": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineCommandPayload"
                    },
                    "type": "array"
                },
                "completed_at": {
                    "type": "string"
                },
                "duration_seconds": {
                    "type": [
                        "number",
                        "null"
                    ]
                },
                "failure": {
                    "$ref": "#/components/schemas/E2EFailureEvidencePayload"
                },
                "kind": {
                    "const": "failed_e2e_test"
                },
                "linked_issues": {
                    "items": {
                        "$ref": "#/components/schemas/LinkedIssueLifecyclePayload"
                    },
                    "type": "array"
                },
                "nodeid": {
                    "type": "string"
                },
                "started_at": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "nodeid",
                "started_at",
                "completed_at",
                "failure",
                "linked_issues",
                "commands"
            ],
            "type": "object"
        },
        "FlowColumnPayload": {
            "additionalProperties": true,
            "properties": {
                "count": {
                    "type": "integer"
                },
                "expandable": {
                    "type": "boolean"
                },
                "hidden_count": {
                    "minimum": 0,
                    "type": "integer"
                },
                "id": {
                    "type": "string"
                },
                "items": {
                    "items": {
                        "$ref": "#/components/schemas/IssueItemPayload"
                    },
                    "type": "array"
                },
                "session_scoped": {
                    "type": "boolean"
                },
                "title": {
                    "type": "string"
                }
            },
            "required": [
                "id",
                "title",
                "count",
                "hidden_count",
                "items"
            ],
            "type": "object"
        },
        "FlowStepPayload": {
            "additionalProperties": false,
            "properties": {
                "key": {
                    "type": "string"
                },
                "label": {
                    "type": "string"
                }
            },
            "required": [
                "key",
                "label"
            ],
            "type": "object"
        },
        "GuardedRecoveryStopActionPayload": {
            "additionalProperties": false,
            "properties": {
                "expected_engine": {
                    "$ref": "#/components/schemas/RecoveryEngineIdentityPayload"
                },
                "expected_owner_fence": {
                    "minimum": 0,
                    "type": "integer"
                },
                "force_on_timeout": {
                    "type": "boolean"
                },
                "graceful_timeout_seconds": {
                    "exclusiveMinimum": 0,
                    "type": "number"
                },
                "record_id": {
                    "minLength": 1,
                    "type": "string"
                }
            },
            "required": [
                "record_id",
                "expected_engine",
                "expected_owner_fence",
                "graceful_timeout_seconds",
                "force_on_timeout"
            ],
            "type": "object"
        },
        "HealthStatus": {
            "enum": [
                "ok",
                "warning",
                "error",
                "info"
            ]
        },
        "HistoricalIntakeCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "actor": {
                    "minLength": 1,
                    "type": "string"
                },
                "branch_name": {
                    "minLength": 1,
                    "type": "string"
                },
                "candidate_path": {
                    "pattern": "^/",
                    "type": "string"
                },
                "candidate_sha256": {
                    "pattern": "^[0-9a-f]{64}$",
                    "type": "string"
                },
                "issue_number": {
                    "minimum": 1,
                    "type": "integer"
                },
                "reason": {
                    "minLength": 1,
                    "type": "string"
                },
                "repo_slug": {
                    "minLength": 1,
                    "type": "string"
                },
                "target_head_sha": {
                    "pattern": "^[0-9a-f]{40}$",
                    "type": "string"
                }
            },
            "required": [
                "repo_slug",
                "issue_number",
                "branch_name",
                "target_head_sha",
                "candidate_path",
                "candidate_sha256",
                "actor",
                "reason"
            ],
            "type": "object"
        },
        "HistoricalIntakeOutcomePayload": {
            "discriminator": {
                "propertyName": "status"
            },
            "oneOf": [
                {
                    "$ref": "#/components/schemas/HistoricalIntakeParkedPayload"
                },
                {
                    "$ref": "#/components/schemas/HistoricalIntakeRefusedPayload"
                },
                {
                    "$ref": "#/components/schemas/HistoricalIntakeValidationFailedPayload"
                }
            ]
        },
        "HistoricalIntakeParkedPayload": {
            "additionalProperties": false,
            "properties": {
                "evidence_id": {
                    "pattern": "^e1:[0-9a-f]{64}$",
                    "type": "string"
                },
                "record_id": {
                    "pattern": "^r1:[0-9a-f]{64}$",
                    "type": "string"
                },
                "status": {
                    "enum": [
                        "parked"
                    ],
                    "type": "string"
                }
            },
            "required": [
                "status",
                "record_id",
                "evidence_id"
            ],
            "type": "object"
        },
        "HistoricalIntakeRefusedPayload": {
            "additionalProperties": false,
            "properties": {
                "reason": {
                    "enum": [
                        "wrong_repository",
                        "candidate_changed",
                        "invalid_completion",
                        "invalid_selection",
                        "prerequisite_unavailable"
                    ],
                    "type": "string"
                },
                "status": {
                    "enum": [
                        "refused"
                    ],
                    "type": "string"
                }
            },
            "required": [
                "status",
                "reason"
            ],
            "type": "object"
        },
        "HistoricalIntakeValidationFailedPayload": {
            "additionalProperties": false,
            "properties": {
                "entry_id": {
                    "pattern": "^[0-9a-f]{64}$",
                    "type": "string"
                },
                "status": {
                    "enum": [
                        "validation_failed"
                    ],
                    "type": "string"
                },
                "validation_path": {
                    "minLength": 1,
                    "type": "string"
                },
                "validation_sha256": {
                    "pattern": "^[0-9a-f]{64}$",
                    "type": "string"
                }
            },
            "required": [
                "status",
                "entry_id",
                "validation_sha256",
                "validation_path"
            ],
            "type": "object"
        },
        "InfoDialogPayload": {
            "additionalProperties": false,
            "properties": {
                "rows": {
                    "items": {
                        "$ref": "#/components/schemas/DialogRowPayload"
                    },
                    "type": "array"
                },
                "title": {
                    "type": "string"
                }
            },
            "required": [
                "title",
                "rows"
            ],
            "type": "object"
        },
        "IssueCyclePayload": {
            "additionalProperties": false,
            "properties": {
                "agent": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "artifacts": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/CycleArtifactsPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "coder": {
                    "$ref": "#/components/schemas/CodingAttemptPayload"
                },
                "cycle_in_run": {
                    "type": [
                        "integer",
                        "null"
                    ]
                },
                "cycle_label": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "cycle_number": {
                    "type": "integer"
                },
                "diagnostics": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineDiagnosticPayload"
                    },
                    "type": "array"
                },
                "expanded": {
                    "type": [
                        "boolean",
                        "null"
                    ]
                },
                "iteration": {
                    "type": [
                        "integer",
                        "null"
                    ]
                },
                "lifecycle": {
                    "type": [
                        "integer",
                        "null"
                    ]
                },
                "outcome": {
                    "$ref": "#/components/schemas/OutcomeBadgePayload"
                },
                "phase_groups": {
                    "items": {
                        "$ref": "#/components/schemas/JourneyPhaseGroupPayload"
                    },
                    "type": "array"
                },
                "reset_from_scratch": {
                    "type": [
                        "boolean",
                        "null"
                    ]
                },
                "retry_count": {
                    "type": [
                        "integer",
                        "null"
                    ]
                },
                "review": {
                    "$ref": "#/components/schemas/ReviewStagePayload"
                },
                "reviewer_agent": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "run_id": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "session_run_ids": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "steps": {
                    "items": {
                        "$ref": "#/components/schemas/JourneyStepPayload"
                    },
                    "type": "array"
                },
                "time_label": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "timestamp": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "validation": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/CycleValidationBadgePayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                }
            },
            "required": [
                "cycle_number",
                "coder",
                "review",
                "outcome",
                "diagnostics",
                "lifecycle",
                "iteration",
                "run_id",
                "timestamp",
                "session_run_ids",
                "agent",
                "reviewer_agent",
                "retry_count",
                "reset_from_scratch",
                "cycle_label",
                "time_label",
                "expanded",
                "cycle_in_run",
                "artifacts",
                "steps",
                "phase_groups",
                "validation"
            ],
            "type": "object"
        },
        "IssueDetailActionPayload": {
            "additionalProperties": false,
            "properties": {
                "id": {
                    "type": "string"
                },
                "label": {
                    "type": "string"
                },
                "run_dir": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "url": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "id",
                "label"
            ],
            "type": "object"
        },
        "IssueDetailBlockedDetailPayload": {
            "additionalProperties": false,
            "properties": {
                "event_summary": {
                    "type": "string"
                },
                "labels": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "reason": {
                    "type": "string"
                },
                "rework_info": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "reason",
                "labels",
                "rework_info",
                "event_summary"
            ],
            "type": "object"
        },
        "IssueDetailPayload": {
            "additionalProperties": false,
            "properties": {
                "actions": {
                    "items": {
                        "$ref": "#/components/schemas/IssueDetailActionPayload"
                    },
                    "type": "array"
                },
                "attempt_count": {
                    "type": "integer"
                },
                "attempts": {
                    "items": {
                        "$ref": "#/components/schemas/AttemptPayload"
                    },
                    "type": "array"
                },
                "blocked_detail": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/IssueDetailBlockedDetailPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "cycles": {
                    "items": {
                        "additionalProperties": true,
                        "type": "object"
                    },
                    "type": "array"
                },
                "e2e_run_id": {
                    "type": [
                        "integer",
                        "null"
                    ]
                },
                "events": {
                    "items": {
                        "additionalProperties": true,
                        "type": "object"
                    },
                    "type": "array"
                },
                "issue_number": {
                    "type": "integer"
                },
                "issue_url": {
                    "type": "string"
                },
                "lifecycle": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/LifecycleTimelineContainerPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "phase_toc": {
                    "items": {
                        "additionalProperties": true,
                        "type": "object"
                    },
                    "type": "array"
                },
                "previous_runs": {
                    "items": {
                        "additionalProperties": true,
                        "type": "object"
                    },
                    "type": "array"
                },
                "previous_runs_count": {
                    "type": "integer"
                },
                "raw_events_count": {
                    "type": "integer"
                },
                "stack_dependency": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/StackDependencyGateViewPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "status_explanation": {
                    "type": "string"
                },
                "summary": {
                    "$ref": "#/components/schemas/IssueDetailSummaryPayload"
                },
                "timeline_steps": {
                    "items": {
                        "additionalProperties": true,
                        "type": "object"
                    },
                    "type": "array"
                },
                "title": {
                    "type": "string"
                },
                "view": {
                    "$ref": "#/components/schemas/TimelineView"
                }
            },
            "required": [
                "issue_number",
                "title",
                "issue_url",
                "phase_toc",
                "cycles",
                "events",
                "summary",
                "actions",
                "status_explanation",
                "attempts",
                "timeline_steps",
                "attempt_count",
                "previous_runs",
                "previous_runs_count",
                "raw_events_count",
                "blocked_detail"
            ],
            "type": "object"
        },
        "IssueDetailSummaryPayload": {
            "additionalProperties": false,
            "properties": {
                "event_count": {
                    "type": "integer"
                },
                "last_event": {
                    "type": "string"
                },
                "run_diagnostic": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/IssueDetailValidationDiagnosticPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "status": {
                    "type": "string"
                },
                "timeline_diagnostic": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/IssueDetailTimelineDiagnosticPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                }
            },
            "required": [
                "status",
                "last_event",
                "event_count"
            ],
            "type": "object"
        },
        "IssueDetailTimelineDiagnosticPayload": {
            "additionalProperties": false,
            "properties": {
                "dropped_missing_semantics": {
                    "type": "integer"
                },
                "expected_timeline_store": {
                    "type": "string"
                },
                "expected_timeline_store_exists": {
                    "type": "boolean"
                },
                "resolved_run_dir": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "signals": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "state": {
                    "type": "string"
                }
            },
            "required": [
                "state",
                "signals",
                "expected_timeline_store",
                "expected_timeline_store_exists",
                "resolved_run_dir",
                "dropped_missing_semantics"
            ],
            "type": "object"
        },
        "IssueDetailValidationDiagnosticPayload": {
            "additionalProperties": false,
            "properties": {
                "command": {
                    "type": "string"
                },
                "exit_code": {
                    "type": "integer"
                },
                "failed_tests": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "failed_tests_preview": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "junit_cases": {
                    "items": {
                        "$ref": "#/components/schemas/TestCaseResultPayload"
                    },
                    "type": "array"
                },
                "reason": {
                    "type": "string"
                },
                "run_dir": {
                    "type": "string"
                },
                "session_name": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "state": {
                    "type": "string"
                },
                "suite": {
                    "type": "string"
                },
                "validation_record_path": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "validation_stderr": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "validation_stdout": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "state",
                "run_dir",
                "session_name",
                "reason",
                "suite",
                "command",
                "exit_code",
                "failed_tests",
                "failed_tests_preview",
                "validation_record_path",
                "validation_stderr",
                "validation_stdout",
                "junit_cases"
            ],
            "type": "object"
        },
        "IssueItemPayload": {
            "additionalProperties": true,
            "properties": {
                "action": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "action_hint": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "issue_number": {
                    "type": [
                        "integer",
                        "string",
                        "null"
                    ]
                },
                "issue_url": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "open_run_command": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/OpenE2ERunCommandPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "provider_badge": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/ProviderBadgeViewPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "provider_signal": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "runtime_label": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "show_stale_badge": {
                    "type": "boolean"
                },
                "stack_chip": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/StackChipViewPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "stack_dependency": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/StackDependencyGateViewPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "stack_signal": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "status": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "title": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "url": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "show_stale_badge"
            ],
            "type": "object"
        },
        "IssueLifecyclePayload": {
            "additionalProperties": false,
            "properties": {
                "cycles": {
                    "items": {
                        "$ref": "#/components/schemas/IssueCyclePayload"
                    },
                    "type": "array"
                },
                "diagnostics": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineDiagnosticPayload"
                    },
                    "type": "array"
                },
                "issue_number": {
                    "type": "integer"
                },
                "title": {
                    "type": "string"
                }
            },
            "required": [
                "issue_number",
                "title",
                "cycles",
                "diagnostics"
            ],
            "type": "object"
        },
        "IssueNumbersRequestPayload": {
            "additionalProperties": false,
            "properties": {
                "issues": {
                    "items": {
                        "minimum": 1,
                        "type": "integer"
                    },
                    "type": "array"
                }
            },
            "required": [
                "issues"
            ],
            "type": "object"
        },
        "IssueRowPayload": {
            "additionalProperties": false,
            "properties": {
                "html": {
                    "type": "string"
                },
                "issue_number": {
                    "type": [
                        "integer",
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "html"
            ],
            "type": "object"
        },
        "IssueRowsPayload": {
            "additionalProperties": false,
            "properties": {
                "active_tab": {
                    "type": "string"
                },
                "count": {
                    "type": "integer"
                },
                "rows": {
                    "items": {
                        "$ref": "#/components/schemas/IssueRowPayload"
                    },
                    "type": "array"
                }
            },
            "required": [
                "rows",
                "active_tab",
                "count"
            ],
            "type": "object"
        },
        "JUnitCasePayload": {
            "additionalProperties": false,
            "properties": {
                "case_id": {
                    "type": "string"
                },
                "display_name": {
                    "type": "string"
                },
                "duration_seconds": {
                    "type": [
                        "number",
                        "null"
                    ]
                },
                "extras": {
                    "items": {
                        "$ref": "#/components/schemas/ValidationExtraPayload"
                    },
                    "type": "array"
                },
                "failure_details": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "outcome": {
                    "enum": [
                        "passed",
                        "failed",
                        "error",
                        "skipped"
                    ],
                    "type": "string"
                },
                "suite_name": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "system_err": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "system_out": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "case_id",
                "display_name",
                "outcome",
                "extras"
            ],
            "type": "object"
        },
        "JourneyPhaseGroupPayload": {
            "additionalProperties": false,
            "properties": {
                "key": {
                    "enum": [
                        "coding",
                        "review",
                        "rework",
                        "orchestrator"
                    ]
                },
                "label": {
                    "type": "string"
                },
                "steps": {
                    "items": {
                        "$ref": "#/components/schemas/JourneyStepPayload"
                    },
                    "type": "array"
                }
            },
            "required": [
                "key",
                "label",
                "steps"
            ],
            "type": "object"
        },
        "JourneyStepPayload": {
            "additionalProperties": false,
            "properties": {
                "actions": {
                    "items": {
                        "additionalProperties": true,
                        "type": "object"
                    },
                    "type": "array"
                },
                "day": {
                    "type": "string"
                },
                "detail": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "event": {
                    "type": "string"
                },
                "in_round_progress": {
                    "type": "boolean"
                },
                "narrative": {
                    "type": "string"
                },
                "status": {
                    "type": "string"
                },
                "time_label": {
                    "type": "string"
                },
                "timestamp": {
                    "type": "string"
                }
            },
            "required": [
                "timestamp",
                "time_label",
                "day",
                "narrative",
                "status",
                "event",
                "actions"
            ],
            "type": "object"
        },
        "LifecycleCommandPayload": {
            "anyOf": [
                {
                    "$ref": "#/components/schemas/TimelineCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/DialogActionCommandPayload"
                }
            ],
            "discriminator": {
                "propertyName": "kind"
            }
        },
        "LifecycleTimelineContainerPayload": {
            "discriminator": {
                "propertyName": "kind"
            },
            "oneOf": [
                {
                    "$ref": "#/components/schemas/DashboardTimelineContainerPayload"
                },
                {
                    "$ref": "#/components/schemas/E2ESuiteTimelineContainerPayload"
                }
            ]
        },
        "LinkedIssueLifecyclePayload": {
            "additionalProperties": false,
            "properties": {
                "command": {
                    "$ref": "#/components/schemas/OpenIssueTimelineCommandPayload"
                },
                "issue_number": {
                    "type": "integer"
                },
                "relationship": {
                    "enum": [
                        "exercises",
                        "discovered",
                        "failed_with",
                        "validates"
                    ]
                }
            },
            "required": [
                "issue_number",
                "relationship",
                "command"
            ],
            "type": "object"
        },
        "MissingCodingEvidencePayload": {
            "additionalProperties": false,
            "properties": {
                "commands": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineCommandPayload"
                    },
                    "type": "array"
                },
                "diagnostics": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineDiagnosticPayload"
                    },
                    "type": "array"
                },
                "expected_state": {
                    "enum": [
                        "completed",
                        "running",
                        "blocked",
                        "failed"
                    ]
                },
                "issue_number": {
                    "type": "integer"
                },
                "kind": {
                    "const": "missing_coding_evidence"
                },
                "missing": {
                    "items": {
                        "$ref": "#/components/schemas/MissingEvidencePayload"
                    },
                    "type": "array"
                },
                "observed_at": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "issue_number",
                "expected_state",
                "observed_at",
                "missing",
                "diagnostics",
                "commands"
            ],
            "type": "object"
        },
        "MissingE2ETestEvidencePayload": {
            "additionalProperties": false,
            "properties": {
                "commands": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineCommandPayload"
                    },
                    "type": "array"
                },
                "diagnostics": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineDiagnosticPayload"
                    },
                    "type": "array"
                },
                "kind": {
                    "const": "missing_e2e_test_evidence"
                },
                "missing": {
                    "items": {
                        "$ref": "#/components/schemas/MissingEvidencePayload"
                    },
                    "type": "array"
                },
                "nodeid": {
                    "type": "string"
                },
                "observed_at": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "nodeid",
                "observed_at",
                "missing",
                "diagnostics",
                "commands"
            ],
            "type": "object"
        },
        "MissingEvidencePayload": {
            "additionalProperties": false,
            "properties": {
                "evidence": {
                    "type": "string"
                },
                "expected_ref": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "kind": {
                    "const": "missing_evidence"
                },
                "reason": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "evidence",
                "reason"
            ],
            "type": "object"
        },
        "MissingReviewEvidencePayload": {
            "additionalProperties": false,
            "properties": {
                "commands": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineCommandPayload"
                    },
                    "type": "array"
                },
                "diagnostics": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineDiagnosticPayload"
                    },
                    "type": "array"
                },
                "expected_state": {
                    "enum": [
                        "approved",
                        "changes_requested",
                        "running"
                    ]
                },
                "kind": {
                    "const": "missing_review_evidence"
                },
                "missing": {
                    "items": {
                        "$ref": "#/components/schemas/MissingEvidencePayload"
                    },
                    "type": "array"
                },
                "observed_at": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "expected_state",
                "observed_at",
                "missing",
                "diagnostics",
                "commands"
            ],
            "type": "object"
        },
        "OpenCompletionRecordCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "kind": {
                    "const": "open_completion_record"
                },
                "label": {
                    "type": "string"
                },
                "path": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "label",
                "path"
            ],
            "type": "object"
        },
        "OpenE2ERunCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "expand_run_details": {
                    "type": "boolean"
                },
                "kind": {
                    "const": "open_e2e_run"
                },
                "label": {
                    "type": "string"
                },
                "run_id": {
                    "minimum": 1,
                    "type": "integer"
                }
            },
            "required": [
                "kind",
                "label",
                "run_id"
            ],
            "type": "object"
        },
        "OpenInlineAgentAttemptsCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "issue_number": {
                    "minimum": 1,
                    "type": "integer"
                },
                "kind": {
                    "const": "open_inline_agent_attempts"
                },
                "label": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "label",
                "issue_number"
            ],
            "type": "object"
        },
        "OpenIssueTimelineCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "e2e_run_id": {
                    "type": [
                        "integer",
                        "null"
                    ]
                },
                "issue_number": {
                    "type": "integer"
                },
                "kind": {
                    "const": "open_issue_timeline"
                },
                "label": {
                    "type": "string"
                },
                "scope_kind": {
                    "enum": [
                        "dashboard",
                        "e2e_run"
                    ]
                }
            },
            "required": [
                "kind",
                "label",
                "issue_number",
                "scope_kind"
            ],
            "type": "object"
        },
        "OpenOrchestratorLogCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "error_surface": {
                    "enum": [
                        "toast",
                        "inline"
                    ]
                },
                "issue_number": {
                    "type": "integer"
                },
                "kind": {
                    "const": "open_orchestrator_log"
                },
                "label": {
                    "type": "string"
                },
                "run_dir": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "kind",
                "label",
                "issue_number",
                "error_surface"
            ],
            "type": "object"
        },
        "OpenPathCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "kind": {
                    "const": "open_path"
                },
                "label": {
                    "type": "string"
                },
                "path": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "label",
                "path"
            ],
            "type": "object"
        },
        "OpenReviewArtifactCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "artifact_path": {
                    "type": "string"
                },
                "artifact_type": {
                    "enum": [
                        "review_report",
                        "review_decision",
                        "tech_lead_report",
                        "tech_lead_decision"
                    ]
                },
                "issue_number": {
                    "type": "integer"
                },
                "kind": {
                    "const": "open_review_artifact"
                },
                "label": {
                    "type": "string"
                },
                "render_mode": {
                    "enum": [
                        "markdown",
                        "json"
                    ]
                },
                "run_dir": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "label",
                "issue_number",
                "run_dir",
                "artifact_path",
                "artifact_type",
                "render_mode"
            ],
            "type": "object"
        },
        "OpenReviewFeedbackCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "event_ref": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "issue_number": {
                    "type": "integer"
                },
                "kind": {
                    "const": "open_review_feedback"
                },
                "label": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "label",
                "issue_number"
            ],
            "type": "object"
        },
        "OpenSessionDiagnosticsCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "issue_number": {
                    "type": "integer"
                },
                "kind": {
                    "const": "open_session_diagnostics"
                },
                "label": {
                    "type": "string"
                },
                "run_dir": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "kind",
                "label",
                "issue_number"
            ],
            "type": "object"
        },
        "OpenSessionRecordingCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "error_surface": {
                    "enum": [
                        "toast",
                        "inline",
                        null
                    ],
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "issue_number": {
                    "type": "integer"
                },
                "kind": {
                    "const": "open_session_recording"
                },
                "label": {
                    "type": "string"
                },
                "round_index": {
                    "type": [
                        "integer",
                        "null"
                    ]
                },
                "run_dir": {
                    "type": "string"
                },
                "session_role": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "kind",
                "label",
                "issue_number",
                "run_dir"
            ],
            "type": "object"
        },
        "OpenValidationDetailsCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "issue_number": {
                    "type": "integer"
                },
                "kind": {
                    "const": "open_validation_details"
                },
                "label": {
                    "type": "string"
                },
                "run_dir": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "label",
                "issue_number",
                "run_dir"
            ],
            "type": "object"
        },
        "OrchestratorInfoPayload": {
            "additionalProperties": false,
            "properties": {
                "active_sessions": {
                    "type": "integer"
                },
                "client_capabilities": {
                    "$ref": "#/components/schemas/ClientCapabilitiesPayload"
                },
                "commit_sha": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "commit_short": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "completed_today": {
                    "type": "integer"
                },
                "config_fingerprint": {
                    "type": "string"
                },
                "config_name": {
                    "type": "string"
                },
                "configuration_mode": {
                    "type": "string"
                },
                "max_sessions": {
                    "type": "integer"
                },
                "repo": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "repo_identity": {
                    "$ref": "#/components/schemas/RepoIdentityPayload"
                },
                "repo_root": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "startup_status": {
                    "$ref": "#/components/schemas/StartupStatus"
                },
                "terminal_backend": {
                    "type": "string"
                },
                "ui_mode": {
                    "type": "string"
                },
                "version": {
                    "type": "string"
                }
            },
            "required": [
                "version",
                "repo",
                "repo_root",
                "configuration_mode",
                "config_name",
                "config_fingerprint",
                "ui_mode",
                "terminal_backend",
                "client_capabilities",
                "commit_sha",
                "commit_short",
                "repo_identity",
                "max_sessions",
                "active_sessions",
                "completed_today",
                "startup_status"
            ],
            "type": "object"
        },
        "OrchestratorStatusPayload": {
            "additionalProperties": false,
            "properties": {
                "active_sessions": {
                    "items": {
                        "$ref": "#/components/schemas/ActiveSessionSummaryPayload"
                    },
                    "type": "array"
                },
                "completed_today": {
                    "items": {
                        "type": "integer"
                    },
                    "type": "array"
                },
                "e2e_role": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "last_tick_time": {
                    "type": [
                        "integer",
                        "number",
                        "null"
                    ]
                },
                "max_sessions": {
                    "type": "integer"
                },
                "pause_actor": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "pause_detail": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "pause_is_incident": {
                    "type": "boolean"
                },
                "pause_reason": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "paused": {
                    "type": "boolean"
                },
                "paused_held_seconds": {
                    "type": "number"
                },
                "paused_since": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "pending_reviews": {
                    "items": {
                        "$ref": "#/components/schemas/PendingReviewSummaryPayload"
                    },
                    "type": "array"
                },
                "queue": {
                    "items": {
                        "type": "integer"
                    },
                    "type": "array"
                },
                "shutdown_requested": {
                    "type": "boolean"
                },
                "startup_status": {
                    "$ref": "#/components/schemas/StartupStatus"
                },
                "tick_id": {
                    "type": [
                        "integer",
                        "number",
                        "null"
                    ]
                }
            },
            "required": [
                "paused",
                "pause_reason",
                "pause_actor",
                "pause_detail",
                "paused_since",
                "paused_held_seconds",
                "pause_is_incident",
                "shutdown_requested",
                "startup_status",
                "active_sessions",
                "max_sessions",
                "completed_today",
                "queue",
                "pending_reviews",
                "tick_id",
                "last_tick_time",
                "e2e_role"
            ],
            "type": "object"
        },
        "OutcomeBadgePayload": {
            "additionalProperties": false,
            "properties": {
                "label": {
                    "type": "string"
                },
                "tone": {
                    "enum": [
                        "passed",
                        "failed",
                        "error",
                        "in_progress",
                        "neutral"
                    ]
                }
            },
            "required": [
                "label",
                "tone"
            ],
            "type": "object"
        },
        "OwnedRecoveryRecordPayload": {
            "additionalProperties": false,
            "properties": {
                "kind": {
                    "const": "owned"
                },
                "owner": {
                    "$ref": "#/components/schemas/RecoveryClaimOwnerPayload"
                },
                "stop_action": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/GuardedRecoveryStopActionPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "work": {
                    "$ref": "#/components/schemas/RecoveryRecordFactPayload"
                }
            },
            "required": [
                "kind",
                "work",
                "owner",
                "stop_action"
            ],
            "type": "object"
        },
        "PassedE2ETestExecutionPayload": {
            "additionalProperties": false,
            "properties": {
                "commands": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineCommandPayload"
                    },
                    "type": "array"
                },
                "completed_at": {
                    "type": "string"
                },
                "duration_seconds": {
                    "type": [
                        "number",
                        "null"
                    ]
                },
                "kind": {
                    "const": "passed_e2e_test"
                },
                "linked_issues": {
                    "items": {
                        "$ref": "#/components/schemas/LinkedIssueLifecyclePayload"
                    },
                    "type": "array"
                },
                "nodeid": {
                    "type": "string"
                },
                "started_at": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "nodeid",
                "started_at",
                "completed_at",
                "linked_issues",
                "commands"
            ],
            "type": "object"
        },
        "PendingReviewSummaryPayload": {
            "additionalProperties": false,
            "properties": {
                "branch_name": {
                    "type": "string"
                },
                "issue_number": {
                    "type": "integer"
                },
                "pr_number": {
                    "type": "integer"
                },
                "pr_url": {
                    "type": "string"
                }
            },
            "required": [
                "issue_number",
                "pr_number",
                "pr_url",
                "branch_name"
            ],
            "type": "object"
        },
        "PhaseDialogPayload": {
            "additionalProperties": false,
            "properties": {
                "issue_number": {
                    "type": "integer"
                },
                "phase": {
                    "additionalProperties": true,
                    "type": [
                        "object",
                        "null"
                    ]
                },
                "phases": {
                    "items": {
                        "additionalProperties": true,
                        "type": "object"
                    },
                    "type": "array"
                },
                "title": {
                    "type": "string"
                }
            },
            "required": [
                "title",
                "issue_number",
                "phase",
                "phases"
            ],
            "type": "object"
        },
        "ProviderBadgeViewPayload": {
            "additionalProperties": false,
            "properties": {
                "label_text": {
                    "type": "string"
                },
                "title": {
                    "type": "string"
                },
                "tone": {
                    "type": "string"
                }
            },
            "required": [
                "tone",
                "label_text",
                "title"
            ],
            "type": "object"
        },
        "ProviderCircuitEntryPayload": {
            "additionalProperties": false,
            "properties": {
                "consecutive_outages": {
                    "type": "integer"
                },
                "cooldown_remaining_label": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "is_open": {
                    "type": "boolean"
                },
                "last_error_summary": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "next_retry_at": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "provider": {
                    "type": "string"
                },
                "status_label": {
                    "type": "string"
                }
            },
            "required": [
                "provider",
                "is_open",
                "status_label",
                "cooldown_remaining_label",
                "next_retry_at",
                "consecutive_outages",
                "last_error_summary"
            ],
            "type": "object"
        },
        "ProviderCircuitStatusPayload": {
            "additionalProperties": false,
            "properties": {
                "any_open": {
                    "type": "boolean"
                },
                "entries": {
                    "items": {
                        "$ref": "#/components/schemas/ProviderCircuitEntryPayload"
                    },
                    "type": "array"
                },
                "next_retry_at": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "open_count": {
                    "type": "integer"
                },
                "open_providers": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "status_unavailable": {
                    "type": "boolean"
                },
                "summary_text": {
                    "type": "string"
                }
            },
            "required": [
                "any_open",
                "open_count",
                "open_providers",
                "summary_text",
                "next_retry_at",
                "entries",
                "status_unavailable"
            ],
            "type": "object"
        },
        "PublishFailedCodingAttemptPayload": {
            "additionalProperties": false,
            "properties": {
                "agent": {
                    "$ref": "#/components/schemas/AgentIdentityPayload"
                },
                "commands": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineCommandPayload"
                    },
                    "type": "array"
                },
                "completed_at": {
                    "type": "string"
                },
                "completion_record": {
                    "$ref": "#/components/schemas/CompletionRecordEvidencePayload"
                },
                "diagnostics": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineDiagnosticPayload"
                    },
                    "type": "array"
                },
                "issue_number": {
                    "type": "integer"
                },
                "kind": {
                    "const": "publish_failed_coding_attempt"
                },
                "outputs": {
                    "$ref": "#/components/schemas/CodingOutputsPayload"
                },
                "publish_failed_at": {
                    "type": "string"
                },
                "reason": {
                    "type": "string"
                },
                "session_recording": {
                    "$ref": "#/components/schemas/SessionRecordingEvidencePayload"
                },
                "started_at": {
                    "type": "string"
                },
                "validation": {
                    "$ref": "#/components/schemas/ValidationOutcomePayload"
                }
            },
            "required": [
                "kind",
                "issue_number",
                "agent",
                "started_at",
                "completed_at",
                "publish_failed_at",
                "reason",
                "completion_record",
                "validation",
                "session_recording",
                "outputs",
                "diagnostics",
                "commands"
            ],
            "type": "object"
        },
        "RawConfigPayload": {
            "additionalProperties": false,
            "properties": {
                "config": {
                    "type": "string"
                }
            },
            "required": [
                "config"
            ],
            "type": "object"
        },
        "RecentE2ERunSummaryPayload": {
            "additionalProperties": false,
            "properties": {
                "branch": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "command_summary": {
                    "type": "string"
                },
                "commit_sha": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "duration_seconds": {
                    "type": [
                        "number",
                        "null"
                    ]
                },
                "expand_command": {
                    "$ref": "#/components/schemas/ExpandE2ERunCommandPayload"
                },
                "finished_at": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "note": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "outcome": {
                    "$ref": "#/components/schemas/OutcomeBadgePayload"
                },
                "results": {
                    "$ref": "#/components/schemas/E2ERunResultCountsPayload"
                },
                "run_id": {
                    "minimum": 1,
                    "type": "integer"
                },
                "runner_kind": {
                    "type": "string"
                },
                "started_at": {
                    "type": "string"
                }
            },
            "required": [
                "run_id",
                "outcome",
                "started_at",
                "runner_kind",
                "command_summary",
                "results",
                "expand_command"
            ],
            "type": "object"
        },
        "RecentE2ERunsPayload": {
            "additionalProperties": false,
            "properties": {
                "runs": {
                    "items": {
                        "$ref": "#/components/schemas/RecentE2ERunSummaryPayload"
                    },
                    "type": "array"
                }
            },
            "required": [
                "runs"
            ],
            "type": "object"
        },
        "RecoveryAuthorityPayload": {
            "additionalProperties": false,
            "properties": {
                "branch_name": {
                    "minLength": 1,
                    "type": "string"
                },
                "evidence_id": {
                    "minLength": 1,
                    "type": "string"
                },
                "expected_remote_head_sha": {
                    "pattern": "^[0-9a-f]{40}$",
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "issue_number": {
                    "minimum": 1,
                    "type": "integer"
                },
                "observation_revision": {
                    "minimum": 0,
                    "type": "integer"
                },
                "pr_number": {
                    "minimum": 1,
                    "type": [
                        "integer",
                        "null"
                    ]
                },
                "record_id": {
                    "minLength": 1,
                    "type": "string"
                },
                "remote_baseline_status": {
                    "enum": [
                        "observed",
                        "unobserved"
                    ]
                },
                "repo_slug": {
                    "minLength": 1,
                    "type": "string"
                },
                "validated_head_sha": {
                    "pattern": "^[0-9a-f]{40}$",
                    "type": "string"
                }
            },
            "required": [
                "record_id",
                "evidence_id",
                "observation_revision",
                "validated_head_sha",
                "branch_name",
                "repo_slug",
                "issue_number",
                "pr_number",
                "expected_remote_head_sha",
                "remote_baseline_status"
            ],
            "type": "object"
        },
        "RecoveryAvailablePayload": {
            "additionalProperties": false,
            "properties": {
                "engine_groups": {
                    "items": {
                        "$ref": "#/components/schemas/RecoveryEngineGroupPayload"
                    },
                    "type": "array"
                },
                "message": {
                    "minLength": 1,
                    "type": "string"
                },
                "repo_key": {
                    "minLength": 1,
                    "type": "string"
                },
                "status": {
                    "const": "available"
                },
                "unowned_records": {
                    "items": {
                        "$ref": "#/components/schemas/UnownedRecoveryRecordPayload"
                    },
                    "type": "array"
                }
            },
            "required": [
                "repo_key",
                "status",
                "engine_groups",
                "unowned_records",
                "message"
            ],
            "type": "object"
        },
        "RecoveryClaimOwnerPayload": {
            "additionalProperties": false,
            "properties": {
                "engine": {
                    "$ref": "#/components/schemas/RecoveryEngineIdentityPayload"
                },
                "owner_fence": {
                    "minimum": 1,
                    "type": "integer"
                },
                "stop_availability": {
                    "enum": [
                        "available",
                        "remote_host",
                        "exact_target_unavailable"
                    ]
                }
            },
            "required": [
                "engine",
                "owner_fence",
                "stop_availability"
            ],
            "type": "object"
        },
        "RecoveryEmptyPayload": {
            "additionalProperties": false,
            "properties": {
                "engine_groups": {
                    "items": {
                        "$ref": "#/components/schemas/RecoveryEngineGroupPayload"
                    },
                    "maxItems": 0,
                    "type": "array"
                },
                "message": {
                    "minLength": 1,
                    "type": "string"
                },
                "repo_key": {
                    "minLength": 1,
                    "type": "string"
                },
                "status": {
                    "const": "empty"
                },
                "unowned_records": {
                    "items": {
                        "$ref": "#/components/schemas/UnownedRecoveryRecordPayload"
                    },
                    "maxItems": 0,
                    "type": "array"
                }
            },
            "required": [
                "repo_key",
                "status",
                "engine_groups",
                "unowned_records",
                "message"
            ],
            "type": "object"
        },
        "RecoveryEngineGroupPayload": {
            "additionalProperties": false,
            "properties": {
                "engine": {
                    "$ref": "#/components/schemas/RecoveryEngineIdentityPayload"
                },
                "presentation": {
                    "enum": [
                        "observed",
                        "missing",
                        "replaced",
                        "unknown"
                    ]
                },
                "presentation_message": {
                    "minLength": 1,
                    "type": "string"
                },
                "records": {
                    "items": {
                        "$ref": "#/components/schemas/OwnedRecoveryRecordPayload"
                    },
                    "minItems": 1,
                    "type": "array"
                }
            },
            "required": [
                "engine",
                "presentation",
                "presentation_message",
                "records"
            ],
            "type": "object"
        },
        "RecoveryEngineIdentityPayload": {
            "additionalProperties": false,
            "properties": {
                "host": {
                    "minLength": 1,
                    "type": "string"
                },
                "instance_id": {
                    "minLength": 1,
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "label": {
                    "minLength": 1,
                    "type": "string"
                },
                "process": {
                    "$ref": "#/components/schemas/RecoveryProcessIdentityPayload"
                },
                "repo_root": {
                    "minLength": 1,
                    "type": "string"
                }
            },
            "required": [
                "repo_root",
                "instance_id",
                "host",
                "label",
                "process"
            ],
            "type": "object"
        },
        "RecoveryProcessIdentityPayload": {
            "additionalProperties": false,
            "properties": {
                "host": {
                    "minLength": 1,
                    "type": "string"
                },
                "instance_id": {
                    "minLength": 1,
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "pid": {
                    "minimum": 1,
                    "type": "integer"
                },
                "started_at": {
                    "minLength": 1,
                    "type": "string"
                }
            },
            "required": [
                "host",
                "pid",
                "started_at",
                "instance_id"
            ],
            "type": "object"
        },
        "RecoveryRecordFactPayload": {
            "additionalProperties": false,
            "properties": {
                "authority": {
                    "$ref": "#/components/schemas/RecoveryAuthorityPayload"
                },
                "escrow_retained": {
                    "type": "boolean"
                },
                "failure": {
                    "enum": [
                        "escrow_write_failed",
                        "artifact_missing",
                        "artifact_hash_mismatch",
                        "artifact_untrusted_path",
                        "validation_sha_mismatch",
                        "worktree_ahead_of_validation",
                        "ancestor_of_pending_head",
                        "divergent_validated_heads",
                        "awaiting_lineage_predecessor",
                        "remote_baseline_unproven",
                        "authority_snapshot_stale",
                        "duplicate_open_pr",
                        "published_head_lacks_validated_work",
                        "workspace_integrity",
                        "ref_pin_lost",
                        "publish_target_mismatch",
                        "remote_diverged",
                        "remote_head_changed",
                        "remote_unreadable",
                        "pr_closed_or_merged",
                        "pr_branch_mismatch",
                        "issue_unreadable",
                        "runtime_active",
                        "push_failed",
                        "submission_lost",
                        "review_routing_failed",
                        null
                    ]
                },
                "reason": {
                    "type": "string"
                },
                "state": {
                    "enum": [
                        "queued",
                        "parked",
                        "publishing",
                        "recovered",
                        "failed",
                        "abandoned"
                    ]
                }
            },
            "required": [
                "authority",
                "state",
                "failure",
                "reason",
                "escrow_retained"
            ],
            "type": "object"
        },
        "RecoveryUnavailablePayload": {
            "additionalProperties": false,
            "properties": {
                "engine_groups": {
                    "items": {
                        "$ref": "#/components/schemas/RecoveryEngineGroupPayload"
                    },
                    "maxItems": 0,
                    "type": "array"
                },
                "message": {
                    "minLength": 1,
                    "type": "string"
                },
                "repo_key": {
                    "minLength": 1,
                    "type": "string"
                },
                "status": {
                    "enum": [
                        "database_absent",
                        "unreadable",
                        "unsupported_schema"
                    ]
                },
                "unowned_records": {
                    "items": {
                        "$ref": "#/components/schemas/UnownedRecoveryRecordPayload"
                    },
                    "maxItems": 0,
                    "type": "array"
                }
            },
            "required": [
                "repo_key",
                "status",
                "engine_groups",
                "unowned_records",
                "message"
            ],
            "type": "object"
        },
        "RepoIdentityPayload": {
            "additionalProperties": false,
            "properties": {
                "branch": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "commit_sha": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "dirty_fingerprint": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "repo_root": {
                    "type": "string"
                },
                "source_root": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "working_tree_dirty": {
                    "type": "boolean"
                }
            },
            "required": [
                "repo_root",
                "commit_sha",
                "branch",
                "working_tree_dirty",
                "dirty_fingerprint",
                "source_root"
            ],
            "type": "object"
        },
        "RepositorySetupCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "config_name": {
                    "minLength": 1,
                    "pattern": "^[^/\\\\]+(?:\\.yaml)?$",
                    "type": "string"
                },
                "configure_internal_reviewer": {
                    "type": "boolean"
                },
                "configure_reviewer": {
                    "type": "boolean"
                },
                "configure_tech_lead": {
                    "type": "boolean"
                },
                "create_labels": {
                    "type": "boolean"
                },
                "create_prompts": {
                    "type": "boolean"
                },
                "effort": {
                    "enum": [
                        "low",
                        "medium",
                        "high",
                        "xhigh",
                        "max"
                    ],
                    "type": "string"
                },
                "github_authorization": {
                    "$ref": "#/components/schemas/RepositorySetupGitHubAuthorizationPayload"
                },
                "internal_review_instructions": {
                    "minLength": 1,
                    "type": "string"
                },
                "internal_review_max_rounds": {
                    "maximum": 50,
                    "minimum": 1,
                    "type": "integer"
                },
                "model": {
                    "enum": [
                        "haiku",
                        "sonnet",
                        "opus"
                    ],
                    "type": "string"
                },
                "replace_existing": {
                    "type": "boolean"
                },
                "repo_name": {
                    "minLength": 1,
                    "type": "string"
                },
                "repo_root": {
                    "minLength": 1,
                    "type": "string"
                },
                "reviewer_effort": {
                    "enum": [
                        "low",
                        "medium",
                        "high",
                        "xhigh",
                        "max"
                    ],
                    "type": "string"
                },
                "reviewer_model": {
                    "enum": [
                        "haiku",
                        "sonnet",
                        "opus"
                    ],
                    "type": "string"
                },
                "tech_lead_effort": {
                    "enum": [
                        "low",
                        "medium",
                        "high",
                        "xhigh",
                        "max"
                    ],
                    "type": "string"
                },
                "tech_lead_model": {
                    "enum": [
                        "haiku",
                        "sonnet",
                        "opus"
                    ],
                    "type": "string"
                },
                "tech_lead_review_threshold": {
                    "maximum": 50,
                    "minimum": 0,
                    "type": "integer"
                },
                "validation_publish_command": {
                    "minLength": 1,
                    "type": "string"
                },
                "validation_quick_command": {
                    "minLength": 1,
                    "type": "string"
                },
                "worker_agent_label": {
                    "pattern": "^agent:(?!(?:reviewer|tech-lead)$).+",
                    "type": "string"
                },
                "worktree_base": {
                    "minLength": 1,
                    "type": "string"
                }
            },
            "required": [
                "repo_root",
                "repo_name",
                "worker_agent_label",
                "model",
                "effort",
                "configure_reviewer",
                "reviewer_model",
                "reviewer_effort",
                "configure_internal_reviewer",
                "internal_review_max_rounds",
                "internal_review_instructions",
                "validation_quick_command",
                "validation_publish_command",
                "github_authorization",
                "configure_tech_lead",
                "tech_lead_model",
                "tech_lead_effort",
                "tech_lead_review_threshold"
            ],
            "type": "object"
        },
        "RepositorySetupConflictPayload": {
            "additionalProperties": false,
            "properties": {
                "config_path": {
                    "type": "string"
                },
                "detail": {
                    "type": "string"
                },
                "error": {
                    "const": "replace_confirmation_required"
                }
            },
            "required": [
                "error",
                "detail",
                "config_path"
            ],
            "type": "object"
        },
        "RepositorySetupDetectionPayload": {
            "additionalProperties": false,
            "properties": {
                "agent_labels": {
                    "items": {
                        "minLength": 1,
                        "type": "string"
                    },
                    "type": "array"
                },
                "config_path": {
                    "oneOf": [
                        {
                            "minLength": 1,
                            "type": "string"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "existing_config": {
                    "oneOf": [
                        {
                            "additionalProperties": true,
                            "type": "object"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "github_authorization": {
                    "$ref": "#/components/schemas/RepositorySetupGitHubAuthorizationDetectionPayload"
                },
                "github_labels": {
                    "items": {
                        "minLength": 1,
                        "type": "string"
                    },
                    "type": "array"
                },
                "prompt_candidates": {
                    "items": {
                        "minLength": 1,
                        "type": "string"
                    },
                    "type": "array"
                },
                "repo": {
                    "oneOf": [
                        {
                            "minLength": 1,
                            "type": "string"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "repo_root": {
                    "minLength": 1,
                    "type": "string"
                },
                "validation_defaults": {
                    "$ref": "#/components/schemas/RepositorySetupValidationDefaultsPayload"
                },
                "worktree_base_default": {
                    "minLength": 1,
                    "type": "string"
                },
                "worktree_base_resolved": {
                    "minLength": 1,
                    "type": "string"
                }
            },
            "required": [
                "repo_root",
                "repo",
                "existing_config",
                "config_path",
                "github_labels",
                "agent_labels",
                "prompt_candidates",
                "worktree_base_default",
                "worktree_base_resolved",
                "github_authorization",
                "validation_defaults"
            ],
            "type": "object"
        },
        "RepositorySetupFailurePayload": {
            "additionalProperties": false,
            "properties": {
                "applied_files": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "created_labels": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "detail": {
                    "type": "string"
                },
                "error": {
                    "const": "repository_setup_failed"
                },
                "stage": {
                    "enum": [
                        "authorization",
                        "planning",
                        "files",
                        "labels"
                    ],
                    "type": "string"
                }
            },
            "required": [
                "error",
                "stage",
                "detail",
                "applied_files",
                "created_labels"
            ],
            "type": "object"
        },
        "RepositorySetupFilePayload": {
            "additionalProperties": false,
            "properties": {
                "action": {
                    "enum": [
                        "create",
                        "overwrite"
                    ],
                    "type": "string"
                },
                "agent": {
                    "type": "string"
                },
                "path": {
                    "type": "string"
                },
                "size": {
                    "minimum": 0,
                    "type": "integer"
                },
                "type": {
                    "enum": [
                        "prompt"
                    ],
                    "type": "string"
                }
            },
            "required": [
                "path",
                "action"
            ],
            "type": "object"
        },
        "RepositorySetupGitHubAuthorizationDetectionPayload": {
            "additionalProperties": false,
            "properties": {
                "authorization": {
                    "$ref": "#/components/schemas/RepositorySetupGitHubAuthorizationPayload"
                },
                "configuration_error": {
                    "minLength": 1,
                    "type": "string"
                },
                "configured_kind": {
                    "enum": [
                        "detected",
                        "personal",
                        "github_app",
                        "invalid"
                    ],
                    "type": "string"
                },
                "inline_token_migration_required": {
                    "type": "boolean"
                }
            },
            "required": [
                "authorization",
                "configured_kind",
                "inline_token_migration_required"
            ],
            "type": "object"
        },
        "RepositorySetupGitHubAuthorizationPayload": {
            "additionalProperties": false,
            "properties": {
                "api_url": {
                    "minLength": 1,
                    "type": "string"
                },
                "app_client_id": {
                    "minLength": 1,
                    "type": "string"
                },
                "app_id": {
                    "minLength": 1,
                    "type": "string"
                },
                "app_installation_id": {
                    "minLength": 1,
                    "type": "string"
                },
                "app_private_key_env": {
                    "minLength": 1,
                    "type": "string"
                },
                "app_private_key_path": {
                    "minLength": 1,
                    "type": "string"
                },
                "http_timeout_seconds": {
                    "exclusiveMinimum": 0,
                    "type": "number"
                },
                "keyring_service": {
                    "minLength": 1,
                    "type": "string"
                },
                "keyring_username": {
                    "minLength": 1,
                    "type": "string"
                },
                "kind": {
                    "enum": [
                        "detected",
                        "personal",
                        "github_app"
                    ],
                    "type": "string"
                },
                "token_env": {
                    "minLength": 1,
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "api_url",
                "http_timeout_seconds"
            ],
            "type": "object"
        },
        "RepositorySetupGitHubTokenPayload": {
            "additionalProperties": false,
            "properties": {
                "api_url": {
                    "minLength": 1,
                    "type": "string"
                },
                "http_timeout_seconds": {
                    "exclusiveMinimum": 0,
                    "type": "number"
                },
                "repo_name": {
                    "minLength": 1,
                    "type": "string"
                },
                "repo_root": {
                    "minLength": 1,
                    "type": "string"
                },
                "token": {
                    "minLength": 1,
                    "type": "string"
                }
            },
            "required": [
                "repo_root",
                "repo_name",
                "token",
                "api_url",
                "http_timeout_seconds"
            ],
            "type": "object"
        },
        "RepositorySetupGitHubVerificationPayload": {
            "additionalProperties": false,
            "properties": {
                "auth_kind": {
                    "enum": [
                        "personal",
                        "github_app"
                    ],
                    "type": "string"
                },
                "authorization": {
                    "$ref": "#/components/schemas/RepositorySetupGitHubAuthorizationPayload"
                },
                "authorship_notice": {
                    "minLength": 1,
                    "type": "string"
                },
                "identity": {
                    "minLength": 1,
                    "type": "string"
                },
                "repository": {
                    "minLength": 1,
                    "type": "string"
                },
                "required_permissions": {
                    "items": {
                        "minLength": 1,
                        "type": "string"
                    },
                    "type": "array"
                },
                "source": {
                    "minLength": 1,
                    "type": "string"
                },
                "verification_note": {
                    "minLength": 1,
                    "type": "string"
                },
                "verified": {
                    "const": true
                }
            },
            "required": [
                "verified",
                "identity",
                "repository",
                "auth_kind",
                "source",
                "authorship_notice",
                "verification_note",
                "required_permissions",
                "authorization"
            ],
            "type": "object"
        },
        "RepositorySetupGitHubVerifyRequestPayload": {
            "additionalProperties": false,
            "properties": {
                "authorization": {
                    "$ref": "#/components/schemas/RepositorySetupGitHubAuthorizationPayload"
                },
                "repo_name": {
                    "minLength": 1,
                    "type": "string"
                },
                "repo_root": {
                    "minLength": 1,
                    "type": "string"
                }
            },
            "required": [
                "repo_root",
                "repo_name",
                "authorization"
            ],
            "type": "object"
        },
        "RepositorySetupPrerequisiteCheckPayload": {
            "additionalProperties": false,
            "properties": {
                "detail": {
                    "minLength": 1,
                    "type": "string"
                },
                "name": {
                    "minLength": 1,
                    "type": "string"
                },
                "ok": {
                    "type": "boolean"
                }
            },
            "required": [
                "ok",
                "detail"
            ],
            "type": "object"
        },
        "RepositorySetupPrerequisitesPayload": {
            "additionalProperties": false,
            "properties": {
                "agent_checks": {
                    "items": {
                        "$ref": "#/components/schemas/RepositorySetupPrerequisiteCheckPayload"
                    },
                    "type": "array"
                },
                "all_ok": {
                    "type": "boolean"
                },
                "checks": {
                    "additionalProperties": {
                        "$ref": "#/components/schemas/RepositorySetupPrerequisiteCheckPayload"
                    },
                    "type": "object"
                }
            },
            "required": [
                "all_ok",
                "checks",
                "agent_checks"
            ],
            "type": "object"
        },
        "RepositorySetupPreviewPayload": {
            "additionalProperties": false,
            "properties": {
                "files": {
                    "items": {
                        "$ref": "#/components/schemas/RepositorySetupFilePayload"
                    },
                    "type": "array"
                },
                "github_authorization": {
                    "$ref": "#/components/schemas/RepositorySetupGitHubVerificationPayload"
                },
                "worktree_base": {
                    "minLength": 1,
                    "type": "string"
                },
                "yaml": {
                    "type": "string"
                }
            },
            "required": [
                "yaml",
                "worktree_base",
                "github_authorization",
                "files"
            ],
            "type": "object"
        },
        "RepositorySetupResultPayload": {
            "additionalProperties": false,
            "properties": {
                "config_path": {
                    "type": "string"
                },
                "created_files": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "created_labels": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "status": {
                    "const": "saved"
                }
            },
            "required": [
                "status",
                "config_path",
                "created_files",
                "created_labels"
            ],
            "type": "object"
        },
        "RepositorySetupValidationDefaultsPayload": {
            "additionalProperties": false,
            "properties": {
                "publish_command": {
                    "oneOf": [
                        {
                            "minLength": 1,
                            "type": "string"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "quick_command": {
                    "oneOf": [
                        {
                            "minLength": 1,
                            "type": "string"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "source": {
                    "minLength": 1,
                    "type": "string"
                }
            },
            "required": [
                "quick_command",
                "publish_command",
                "source"
            ],
            "type": "object"
        },
        "RetrospectiveReviewDecisionPayload": {
            "additionalProperties": false,
            "properties": {
                "action": {
                    "type": "string"
                },
                "agent_label": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "eligible": {
                    "type": "boolean"
                },
                "issue": {
                    "minimum": 1,
                    "type": "integer"
                },
                "labels": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "prior_pr_number": {
                    "minimum": 1,
                    "type": [
                        "integer",
                        "null"
                    ]
                },
                "prior_pr_url": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "reason": {
                    "type": "string"
                },
                "state": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "title": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "trigger_label": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "issue",
                "title",
                "state",
                "labels",
                "eligible",
                "action",
                "reason",
                "agent_label",
                "trigger_label",
                "prior_pr_number",
                "prior_pr_url"
            ],
            "type": "object"
        },
        "RetrospectiveReviewExecutePayload": {
            "additionalProperties": false,
            "properties": {
                "failed": {
                    "items": {
                        "$ref": "#/components/schemas/RetrospectiveReviewFailurePayload"
                    },
                    "type": "array"
                },
                "queued": {
                    "items": {
                        "$ref": "#/components/schemas/RetrospectiveReviewQueuedPayload"
                    },
                    "type": "array"
                },
                "refresh_triggered": {
                    "type": "boolean"
                },
                "skipped": {
                    "items": {
                        "$ref": "#/components/schemas/RetrospectiveReviewDecisionPayload"
                    },
                    "type": "array"
                },
                "trigger_label": {
                    "type": "string"
                },
                "workflow": {
                    "const": "retrospective_review"
                }
            },
            "required": [
                "queued",
                "failed",
                "skipped",
                "workflow",
                "trigger_label",
                "refresh_triggered"
            ],
            "type": "object"
        },
        "RetrospectiveReviewFailurePayload": {
            "additionalProperties": false,
            "properties": {
                "error": {
                    "type": "string"
                },
                "issue": {
                    "minimum": 1,
                    "type": "integer"
                }
            },
            "required": [
                "issue",
                "error"
            ],
            "type": "object"
        },
        "RetrospectiveReviewPreflightPayload": {
            "additionalProperties": false,
            "properties": {
                "decisions": {
                    "items": {
                        "$ref": "#/components/schemas/RetrospectiveReviewDecisionPayload"
                    },
                    "type": "array"
                },
                "eligible": {
                    "items": {
                        "minimum": 1,
                        "type": "integer"
                    },
                    "type": "array"
                },
                "skipped": {
                    "items": {
                        "minimum": 1,
                        "type": "integer"
                    },
                    "type": "array"
                },
                "trigger_label": {
                    "type": "string"
                },
                "workflow": {
                    "const": "retrospective_review"
                }
            },
            "required": [
                "decisions",
                "eligible",
                "skipped",
                "workflow",
                "trigger_label"
            ],
            "type": "object"
        },
        "RetrospectiveReviewQueuedPayload": {
            "additionalProperties": false,
            "properties": {
                "action": {
                    "type": "string"
                },
                "agent_label": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "eligible": {
                    "type": "boolean"
                },
                "issue": {
                    "minimum": 1,
                    "type": "integer"
                },
                "labels": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "prior_pr_number": {
                    "minimum": 1,
                    "type": [
                        "integer",
                        "null"
                    ]
                },
                "prior_pr_url": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "queued": {
                    "type": "boolean"
                },
                "reason": {
                    "type": "string"
                },
                "state": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "title": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "trigger_label": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "issue",
                "title",
                "state",
                "labels",
                "eligible",
                "action",
                "reason",
                "agent_label",
                "trigger_label",
                "prior_pr_number",
                "prior_pr_url",
                "queued"
            ],
            "type": "object"
        },
        "ReviewApprovedPayload": {
            "additionalProperties": false,
            "properties": {
                "commands": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineCommandPayload"
                    },
                    "type": "array"
                },
                "completed_at": {
                    "type": "string"
                },
                "kind": {
                    "const": "review_approved"
                },
                "reviewer": {
                    "$ref": "#/components/schemas/AgentIdentityPayload"
                },
                "session_recording": {
                    "$ref": "#/components/schemas/SessionRecordingEvidencePayload"
                },
                "started_at": {
                    "type": "string"
                },
                "transcript": {
                    "$ref": "#/components/schemas/ReviewTranscriptEvidencePayload"
                }
            },
            "required": [
                "kind",
                "reviewer",
                "started_at",
                "completed_at",
                "session_recording",
                "transcript",
                "commands"
            ],
            "type": "object"
        },
        "ReviewChangesRequestedPayload": {
            "additionalProperties": false,
            "properties": {
                "commands": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineCommandPayload"
                    },
                    "type": "array"
                },
                "completed_at": {
                    "type": "string"
                },
                "feedback_summary": {
                    "type": "string"
                },
                "kind": {
                    "const": "review_changes_requested"
                },
                "reviewer": {
                    "$ref": "#/components/schemas/AgentIdentityPayload"
                },
                "session_recording": {
                    "$ref": "#/components/schemas/SessionRecordingEvidencePayload"
                },
                "started_at": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "reviewer",
                "started_at",
                "completed_at",
                "feedback_summary",
                "session_recording",
                "commands"
            ],
            "type": "object"
        },
        "ReviewFailedPayload": {
            "additionalProperties": false,
            "properties": {
                "commands": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineCommandPayload"
                    },
                    "type": "array"
                },
                "diagnostics": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineDiagnosticPayload"
                    },
                    "type": "array"
                },
                "failed_at": {
                    "type": "string"
                },
                "kind": {
                    "const": "review_failed"
                },
                "reason": {
                    "type": "string"
                },
                "reviewer": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/AgentIdentityPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "session_recording": {
                    "$ref": "#/components/schemas/SessionRecordingEvidencePayload"
                },
                "started_at": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "kind",
                "failed_at",
                "reason",
                "session_recording",
                "diagnostics",
                "commands"
            ],
            "type": "object"
        },
        "ReviewFeedbackEntryPayload": {
            "additionalProperties": false,
            "properties": {
                "content": {
                    "type": "string"
                },
                "cycle": {
                    "type": "integer"
                },
                "path": {
                    "type": "string"
                }
            },
            "required": [
                "cycle",
                "path",
                "content"
            ],
            "type": "object"
        },
        "ReviewNotReachedPayload": {
            "additionalProperties": false,
            "properties": {
                "kind": {
                    "const": "review_not_reached"
                },
                "reason": {
                    "enum": [
                        "coding_in_progress",
                        "coding_failed",
                        "publish_failed",
                        "validation_failed",
                        "not_required"
                    ]
                }
            },
            "required": [
                "kind",
                "reason"
            ],
            "type": "object"
        },
        "ReviewRunningPayload": {
            "additionalProperties": false,
            "properties": {
                "commands": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineCommandPayload"
                    },
                    "type": "array"
                },
                "kind": {
                    "const": "review_running"
                },
                "reviewer": {
                    "$ref": "#/components/schemas/AgentIdentityPayload"
                },
                "session_recording": {
                    "$ref": "#/components/schemas/SessionRecordingEvidencePayload"
                },
                "started_at": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "reviewer",
                "started_at",
                "session_recording",
                "commands"
            ],
            "type": "object"
        },
        "ReviewSkippedPayload": {
            "additionalProperties": false,
            "properties": {
                "kind": {
                    "const": "review_skipped"
                },
                "reason": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "reason"
            ],
            "type": "object"
        },
        "ReviewStagePayload": {
            "discriminator": {
                "propertyName": "kind"
            },
            "oneOf": [
                {
                    "$ref": "#/components/schemas/ReviewNotReachedPayload"
                },
                {
                    "$ref": "#/components/schemas/ReviewSkippedPayload"
                },
                {
                    "$ref": "#/components/schemas/ReviewRunningPayload"
                },
                {
                    "$ref": "#/components/schemas/ReviewApprovedPayload"
                },
                {
                    "$ref": "#/components/schemas/ReviewChangesRequestedPayload"
                },
                {
                    "$ref": "#/components/schemas/ReviewFailedPayload"
                },
                {
                    "$ref": "#/components/schemas/MissingReviewEvidencePayload"
                }
            ]
        },
        "ReviewTranscriptAvailablePayload": {
            "additionalProperties": false,
            "properties": {
                "kind": {
                    "const": "available"
                }
            },
            "required": [
                "kind"
            ],
            "type": "object"
        },
        "ReviewTranscriptEvidencePayload": {
            "discriminator": {
                "propertyName": "kind"
            },
            "oneOf": [
                {
                    "$ref": "#/components/schemas/ReviewTranscriptAvailablePayload"
                },
                {
                    "$ref": "#/components/schemas/ReviewTranscriptUnavailablePayload"
                }
            ]
        },
        "ReviewTranscriptUnavailablePayload": {
            "additionalProperties": false,
            "properties": {
                "diagnostics": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineDiagnosticPayload"
                    },
                    "type": "array"
                },
                "kind": {
                    "const": "unavailable"
                },
                "reason": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "reason",
                "diagnostics"
            ],
            "type": "object"
        },
        "ReworkProposalPayload": {
            "additionalProperties": false,
            "properties": {
                "can_approve": {
                    "type": "boolean"
                },
                "can_decline": {
                    "type": "boolean"
                },
                "detail": {
                    "type": "string"
                },
                "evidence_identity": {
                    "type": "string"
                },
                "expected_head": {
                    "type": "string"
                },
                "feedback": {
                    "type": "string"
                },
                "forward_issue_number": {
                    "type": "integer"
                },
                "issue_number": {
                    "type": "integer"
                },
                "mutations": {
                    "type": "string"
                },
                "pr_number": {
                    "type": "integer"
                },
                "proposal_issue_number": {
                    "type": "integer"
                },
                "report": {
                    "type": "string"
                },
                "repository": {
                    "type": "string"
                },
                "status": {
                    "type": "string"
                }
            },
            "required": [
                "proposal_issue_number",
                "pr_number",
                "issue_number",
                "forward_issue_number",
                "repository",
                "expected_head",
                "evidence_identity",
                "feedback",
                "report",
                "status",
                "detail",
                "mutations",
                "can_approve",
                "can_decline"
            ],
            "type": "object"
        },
        "ReworkProposalsPayload": {
            "additionalProperties": false,
            "properties": {
                "proposals": {
                    "items": {
                        "$ref": "#/components/schemas/ReworkProposalPayload"
                    },
                    "type": "array"
                }
            },
            "required": [
                "proposals"
            ],
            "type": "object"
        },
        "RunningCodingAttemptPayload": {
            "additionalProperties": false,
            "properties": {
                "agent": {
                    "$ref": "#/components/schemas/AgentIdentityPayload"
                },
                "commands": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineCommandPayload"
                    },
                    "type": "array"
                },
                "issue_number": {
                    "type": "integer"
                },
                "kind": {
                    "const": "running_coding_attempt"
                },
                "session_recording": {
                    "$ref": "#/components/schemas/SessionRecordingEvidencePayload"
                },
                "started_at": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "issue_number",
                "agent",
                "started_at",
                "session_recording",
                "commands"
            ],
            "type": "object"
        },
        "RunningE2ETestExecutionPayload": {
            "additionalProperties": false,
            "properties": {
                "commands": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineCommandPayload"
                    },
                    "type": "array"
                },
                "kind": {
                    "const": "running_e2e_test"
                },
                "linked_issues": {
                    "items": {
                        "$ref": "#/components/schemas/LinkedIssueLifecyclePayload"
                    },
                    "type": "array"
                },
                "nodeid": {
                    "type": "string"
                },
                "started_at": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "nodeid",
                "started_at",
                "linked_issues",
                "commands"
            ],
            "type": "object"
        },
        "SessionDiagnosticsAnalysisPayload": {
            "additionalProperties": false,
            "properties": {
                "detail": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "headline": {
                    "type": "string"
                },
                "suggestions": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                }
            },
            "required": [
                "headline"
            ],
            "type": "object"
        },
        "SessionDiagnosticsDialogPayload": {
            "additionalProperties": false,
            "properties": {
                "actions": {
                    "items": {
                        "$ref": "#/components/schemas/DialogActionPayload"
                    },
                    "type": "array"
                },
                "analysis": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/SessionDiagnosticsAnalysisPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "follow_up_issues": {
                    "items": {
                        "$ref": "#/components/schemas/SessionDiagnosticsFollowUpIssuePayload"
                    },
                    "type": "array"
                },
                "rows": {
                    "items": {
                        "$ref": "#/components/schemas/DialogRowPayload"
                    },
                    "type": "array"
                },
                "title": {
                    "type": "string"
                }
            },
            "required": [
                "title",
                "rows",
                "actions"
            ],
            "type": "object"
        },
        "SessionDiagnosticsFollowUpIssuePayload": {
            "additionalProperties": false,
            "properties": {
                "blocking": {
                    "type": "boolean"
                },
                "evidence": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "reason": {
                    "type": "string"
                },
                "suggested_labels": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "title": {
                    "type": "string"
                }
            },
            "required": [
                "title",
                "reason",
                "blocking"
            ],
            "type": "object"
        },
        "SessionFailureDiagnosisPayload": {
            "additionalProperties": false,
            "properties": {
                "ai_system": {
                    "type": "string"
                },
                "analysis_detail": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "analysis_headline": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "analysis_suggestions": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "history_reason": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "history_status": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "issue_number": {
                    "type": "integer"
                },
                "log_context": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "log_exists": {
                    "type": "boolean"
                },
                "log_path": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "permission_mode": {
                    "type": "string"
                },
                "review_feedback": {
                    "items": {
                        "$ref": "#/components/schemas/ReviewFeedbackEntryPayload"
                    },
                    "type": "array"
                },
                "suggestions": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "warnings": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "worktree_path": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "issue_number",
                "ai_system",
                "permission_mode",
                "worktree_path",
                "log_path",
                "log_exists",
                "log_context",
                "history_status",
                "history_reason",
                "warnings",
                "suggestions",
                "review_feedback",
                "analysis_headline",
                "analysis_detail",
                "analysis_suggestions"
            ],
            "type": "object"
        },
        "SessionRecordingAvailablePayload": {
            "additionalProperties": false,
            "properties": {
                "command": {
                    "$ref": "#/components/schemas/OpenSessionRecordingCommandPayload"
                },
                "kind": {
                    "const": "available"
                },
                "recording_path": {
                    "type": "string"
                },
                "run_dir": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "run_dir",
                "recording_path",
                "command"
            ],
            "type": "object"
        },
        "SessionRecordingEvidencePayload": {
            "discriminator": {
                "propertyName": "kind"
            },
            "oneOf": [
                {
                    "$ref": "#/components/schemas/SessionRecordingAvailablePayload"
                },
                {
                    "$ref": "#/components/schemas/SessionRecordingUnavailablePayload"
                }
            ]
        },
        "SessionRecordingUnavailablePayload": {
            "additionalProperties": false,
            "properties": {
                "diagnostics": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineDiagnosticPayload"
                    },
                    "type": "array"
                },
                "kind": {
                    "const": "unavailable"
                },
                "reason": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "reason",
                "diagnostics"
            ],
            "type": "object"
        },
        "ShowEventDetailsCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "event_ref": {
                    "type": "string"
                },
                "kind": {
                    "const": "show_event_details"
                },
                "label": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "label",
                "event_ref"
            ],
            "type": "object"
        },
        "StackChipViewPayload": {
            "additionalProperties": false,
            "properties": {
                "mode_label": {
                    "type": "string"
                },
                "status_text": {
                    "type": "string"
                },
                "title": {
                    "type": "string"
                },
                "tone": {
                    "type": "string"
                }
            },
            "required": [
                "tone",
                "mode_label",
                "status_text",
                "title"
            ],
            "type": "object"
        },
        "StackDependencyGatePayload": {
            "additionalProperties": false,
            "properties": {
                "gate": {
                    "type": "string"
                },
                "open": {
                    "type": "boolean"
                },
                "reason_codes": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "reasons": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                }
            },
            "required": [
                "gate",
                "open",
                "reason_codes",
                "reasons"
            ],
            "type": "object"
        },
        "StackDependencyGateViewPayload": {
            "additionalProperties": false,
            "properties": {
                "approval_freshness": {
                    "type": "string"
                },
                "blocked_gates": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "blocked_reason_codes": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "gates": {
                    "items": {
                        "$ref": "#/components/schemas/StackDependencyGatePayload"
                    },
                    "type": "array"
                },
                "has_stack_edges": {
                    "type": "boolean"
                },
                "issue_number": {
                    "type": "integer"
                },
                "mode": {
                    "type": "string"
                },
                "predecessors": {
                    "items": {
                        "$ref": "#/components/schemas/StackDependencyPredecessorPayload"
                    },
                    "type": "array"
                },
                "stack_base_branch": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "stale": {
                    "type": "boolean"
                },
                "stale_reason_codes": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "successors": {
                    "items": {
                        "$ref": "#/components/schemas/StackDependencySuccessorPayload"
                    },
                    "type": "array"
                }
            },
            "required": [
                "issue_number",
                "mode",
                "has_stack_edges",
                "gates",
                "predecessors",
                "successors",
                "blocked_gates",
                "blocked_reason_codes",
                "stale",
                "stale_reason_codes",
                "stack_base_branch",
                "approval_freshness"
            ],
            "type": "object"
        },
        "StackDependencyPredecessorPayload": {
            "additionalProperties": false,
            "properties": {
                "mode": {
                    "type": "string"
                },
                "problem": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "ref": {
                    "type": "string"
                },
                "state": {
                    "type": "string"
                }
            },
            "required": [
                "ref",
                "mode",
                "state",
                "problem"
            ],
            "type": "object"
        },
        "StackDependencySuccessorPayload": {
            "additionalProperties": false,
            "properties": {
                "issue_number": {
                    "type": "integer"
                },
                "mode": {
                    "type": "string"
                },
                "ref": {
                    "type": "string"
                }
            },
            "required": [
                "issue_number",
                "ref",
                "mode"
            ],
            "type": "object"
        },
        "StaleIssuePayload": {
            "additionalProperties": false,
            "properties": {
                "consecutive_ticks": {
                    "type": "integer"
                },
                "issue_number": {
                    "type": "integer"
                },
                "persistent": {
                    "type": "boolean"
                },
                "threshold": {
                    "type": "integer"
                }
            },
            "required": [
                "issue_number",
                "consecutive_ticks",
                "persistent",
                "threshold"
            ],
            "type": "object"
        },
        "StaleIssuesPayload": {
            "additionalProperties": false,
            "properties": {
                "stale": {
                    "additionalProperties": {
                        "$ref": "#/components/schemas/StaleIssuePayload"
                    },
                    "type": "object"
                }
            },
            "required": [
                "stale"
            ],
            "type": "object"
        },
        "StartupStatus": {
            "enum": [
                "pending",
                "running",
                "complete"
            ]
        },
        "StopOwnerAbsentOutcomePayload": {
            "additionalProperties": false,
            "properties": {
                "message": {
                    "minLength": 1,
                    "pattern": ".*\\S.*",
                    "type": "string"
                },
                "observed_owner": {
                    "type": "null"
                },
                "status": {
                    "enum": [
                        "no_such_record",
                        "record_unavailable",
                        "not_owned"
                    ]
                }
            },
            "required": [
                "status",
                "observed_owner",
                "message"
            ],
            "type": "object"
        },
        "StopOwnerObservedOutcomePayload": {
            "additionalProperties": false,
            "properties": {
                "message": {
                    "minLength": 1,
                    "pattern": ".*\\S.*",
                    "type": "string"
                },
                "observed_owner": {
                    "$ref": "#/components/schemas/RecoveryClaimOwnerPayload"
                },
                "status": {
                    "enum": [
                        "stopped",
                        "stop_in_progress",
                        "remote_host",
                        "stop_failed"
                    ]
                }
            },
            "required": [
                "status",
                "observed_owner",
                "message"
            ],
            "type": "object"
        },
        "StopOwnerOptionalOutcomePayload": {
            "additionalProperties": false,
            "properties": {
                "message": {
                    "minLength": 1,
                    "pattern": ".*\\S.*",
                    "type": "string"
                },
                "observed_owner": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/RecoveryClaimOwnerPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "status": {
                    "enum": [
                        "owner_changed",
                        "repo_mismatch"
                    ]
                }
            },
            "required": [
                "status",
                "observed_owner",
                "message"
            ],
            "type": "object"
        },
        "StopValidatedWorkOwnerOutcomePayload": {
            "discriminator": {
                "propertyName": "status"
            },
            "oneOf": [
                {
                    "$ref": "#/components/schemas/StopOwnerObservedOutcomePayload"
                },
                {
                    "$ref": "#/components/schemas/StopOwnerAbsentOutcomePayload"
                },
                {
                    "$ref": "#/components/schemas/StopOwnerOptionalOutcomePayload"
                }
            ]
        },
        "StopValidatedWorkOwnerRequestPayload": {
            "additionalProperties": false,
            "properties": {
                "expected_engine": {
                    "$ref": "#/components/schemas/RecoveryEngineIdentityPayload"
                },
                "expected_owner_fence": {
                    "minimum": 1,
                    "type": "integer"
                },
                "reason": {
                    "minLength": 1,
                    "pattern": ".*\\S.*",
                    "type": "string"
                },
                "record_id": {
                    "minLength": 1,
                    "type": "string"
                }
            },
            "required": [
                "record_id",
                "expected_engine",
                "expected_owner_fence",
                "reason"
            ],
            "type": "object"
        },
        "SwitchE2ETimelineViewCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "kind": {
                    "const": "switch_e2e_timeline_view"
                },
                "label": {
                    "type": "string"
                },
                "run_id": {
                    "minimum": 1,
                    "type": "integer"
                },
                "view": {
                    "$ref": "#/components/schemas/TimelineView"
                }
            },
            "required": [
                "kind",
                "label",
                "run_id",
                "view"
            ],
            "type": "object"
        },
        "TechLeadActivityPayload": {
            "additionalProperties": false,
            "properties": {
                "emptyMessage": {
                    "type": "string"
                },
                "entries": {
                    "items": {
                        "$ref": "#/components/schemas/TechLeadRunActivityEntryPayload"
                    },
                    "type": "array"
                }
            },
            "required": [
                "entries",
                "emptyMessage"
            ],
            "type": "object"
        },
        "TechLeadGlobalHealthReviewScopePayload": {
            "additionalProperties": false,
            "properties": {
                "kind": {
                    "const": "global_health_review"
                }
            },
            "required": [
                "kind"
            ],
            "type": "object"
        },
        "TechLeadIssueScopePayload": {
            "additionalProperties": false,
            "properties": {
                "issue_number": {
                    "minimum": 1,
                    "type": "integer"
                },
                "kind": {
                    "const": "issue"
                }
            },
            "required": [
                "kind",
                "issue_number"
            ],
            "type": "object"
        },
        "TechLeadProposalCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "decision": {
                    "enum": [
                        "approve",
                        "decline"
                    ],
                    "type": "string"
                },
                "proposal_issue_number": {
                    "minimum": 1,
                    "type": "integer"
                }
            },
            "required": [
                "proposal_issue_number",
                "decision"
            ],
            "type": "object"
        },
        "TechLeadProposalOutcomePayload": {
            "additionalProperties": false,
            "properties": {
                "detail": {
                    "type": "string"
                },
                "outcome": {
                    "type": "string"
                },
                "proposal_issue_number": {
                    "type": "integer"
                }
            },
            "required": [
                "proposal_issue_number",
                "outcome",
                "detail"
            ],
            "type": "object"
        },
        "TechLeadRunActivityEntryPayload": {
            "additionalProperties": false,
            "properties": {
                "anchorIssueNumber": {
                    "type": "integer"
                },
                "artifacts": {
                    "items": {
                        "$ref": "#/components/schemas/TechLeadRunArtifactCommandPayload"
                    },
                    "type": "array"
                },
                "artifactsNote": {
                    "type": "string"
                },
                "detail": {
                    "type": "string"
                },
                "endedAt": {
                    "type": "string"
                },
                "findings": {
                    "type": "integer"
                },
                "flavorLabel": {
                    "type": "string"
                },
                "phase": {
                    "enum": [
                        "running",
                        "completed",
                        "needs_human",
                        "failed",
                        "withdrawn"
                    ]
                },
                "phaseLabel": {
                    "type": "string"
                },
                "proposals": {
                    "type": "integer"
                },
                "runId": {
                    "type": "string"
                },
                "runKey": {
                    "type": "string"
                },
                "sessionName": {
                    "type": "string"
                },
                "startedAt": {
                    "type": "string"
                },
                "subjectIssueNumber": {
                    "type": "integer"
                },
                "subjectKind": {
                    "enum": [
                        "issue",
                        "board",
                        "pr_manifest"
                    ]
                },
                "subjectLabel": {
                    "type": "string"
                },
                "subjectTitle": {
                    "type": "string"
                },
                "tone": {
                    "enum": [
                        "active",
                        "good",
                        "warn",
                        "bad",
                        "muted"
                    ]
                }
            },
            "required": [
                "runKey",
                "flavorLabel",
                "phase",
                "phaseLabel",
                "tone",
                "startedAt",
                "endedAt",
                "subjectKind",
                "subjectLabel",
                "subjectIssueNumber",
                "subjectTitle",
                "anchorIssueNumber",
                "detail",
                "findings",
                "proposals",
                "runId",
                "sessionName",
                "artifacts",
                "artifactsNote"
            ],
            "type": "object"
        },
        "TechLeadRunAdmissionPayload": {
            "additionalProperties": false,
            "properties": {
                "admitted": {
                    "type": "boolean"
                },
                "behind_global_barrier": {
                    "type": "boolean"
                },
                "detail": {
                    "type": "string"
                },
                "issue_number": {
                    "minimum": 1,
                    "type": [
                        "integer",
                        "null"
                    ]
                },
                "outcome": {
                    "enum": [
                        "queued",
                        "already_queued",
                        "already_running",
                        "paused",
                        "not_running",
                        "not_configured",
                        "not_eligible",
                        "claim_conflict",
                        "failed"
                    ]
                },
                "reason": {
                    "type": "string"
                },
                "run_key": {
                    "type": "string"
                },
                "scope_kind": {
                    "enum": [
                        "global_health_review",
                        "global_batch_review",
                        "issue"
                    ]
                }
            },
            "required": [
                "outcome",
                "scope_kind",
                "run_key",
                "reason",
                "detail",
                "issue_number",
                "behind_global_barrier",
                "admitted"
            ],
            "type": "object"
        },
        "TechLeadRunArtifactCommandPayload": {
            "discriminator": {
                "propertyName": "kind"
            },
            "oneOf": [
                {
                    "$ref": "#/components/schemas/OpenSessionRecordingCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/OpenReviewArtifactCommandPayload"
                }
            ]
        },
        "TechLeadRunRequestPayload": {
            "additionalProperties": false,
            "properties": {
                "scope": {
                    "$ref": "#/components/schemas/TechLeadRunScopePayload"
                }
            },
            "required": [
                "scope"
            ],
            "type": "object"
        },
        "TechLeadRunScopePayload": {
            "discriminator": {
                "propertyName": "kind"
            },
            "oneOf": [
                {
                    "$ref": "#/components/schemas/TechLeadGlobalHealthReviewScopePayload"
                },
                {
                    "$ref": "#/components/schemas/TechLeadIssueScopePayload"
                }
            ]
        },
        "TestCaseHistoryPayload": {
            "additionalProperties": false,
            "properties": {
                "outcome": {
                    "type": "string"
                },
                "run_id": {
                    "type": "integer"
                }
            },
            "required": [
                "outcome",
                "run_id"
            ],
            "type": "object"
        },
        "TestCaseIssueLinkPayload": {
            "additionalProperties": false,
            "properties": {
                "number": {
                    "type": "integer"
                },
                "resolution": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "status": {
                    "type": "string"
                }
            },
            "required": [
                "number",
                "status",
                "resolution"
            ],
            "type": "object"
        },
        "TestCaseResultPayload": {
            "additionalProperties": false,
            "properties": {
                "captured_output": {
                    "$ref": "#/components/schemas/CapturedOutputAvailabilityPayload"
                },
                "case_id": {
                    "type": "string"
                },
                "category": {
                    "type": "string"
                },
                "display_name": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "duration_seconds": {
                    "type": [
                        "number",
                        "null"
                    ]
                },
                "existing_issue": {
                    "oneOf": [
                        {
                            "$ref": "#/components/schemas/TestCaseIssueLinkPayload"
                        },
                        {
                            "type": "null"
                        }
                    ]
                },
                "failure_summary": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "flip_rate": {
                    "type": "number"
                },
                "flip_rate_percent": {
                    "type": "number"
                },
                "history": {
                    "items": {
                        "$ref": "#/components/schemas/TestCaseHistoryPayload"
                    },
                    "type": "array"
                },
                "is_likely_flaky": {
                    "type": "boolean"
                },
                "is_quarantined": {
                    "type": "boolean"
                },
                "label": {
                    "type": "string"
                },
                "longrepr": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "nodeid": {
                    "type": "string"
                },
                "outcome": {
                    "type": "string"
                },
                "result_category": {
                    "type": "string"
                },
                "result_source": {
                    "type": "string"
                },
                "retry_outcome": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "suite_name": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "updated_at": {
                    "type": "string"
                }
            },
            "required": [
                "nodeid",
                "case_id",
                "label",
                "display_name",
                "suite_name",
                "result_source",
                "outcome",
                "duration_seconds",
                "longrepr",
                "failure_summary",
                "retry_outcome",
                "is_quarantined",
                "updated_at",
                "history",
                "existing_issue",
                "category",
                "result_category",
                "flip_rate",
                "flip_rate_percent",
                "is_likely_flaky",
                "captured_output"
            ],
            "type": "object"
        },
        "TestRunArtifactPayload": {
            "additionalProperties": false,
            "properties": {
                "kind": {
                    "type": "string"
                },
                "label": {
                    "type": "string"
                },
                "path": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "label",
                "path"
            ],
            "type": "object"
        },
        "TimelineCommandPayload": {
            "discriminator": {
                "propertyName": "kind"
            },
            "oneOf": [
                {
                    "$ref": "#/components/schemas/ShowEventDetailsCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/OpenCompletionRecordCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/OpenValidationDetailsCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/OpenSessionRecordingCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/OpenReviewFeedbackCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/OpenReviewArtifactCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/OpenIssueTimelineCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/OpenE2ERunCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/ExpandE2ERunCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/SwitchE2ETimelineViewCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/CreateE2EUntriagedIssuesCommandPayload"
                },
                {
                    "$ref": "#/components/schemas/OpenInlineAgentAttemptsCommandPayload"
                }
            ]
        },
        "TimelineDiagnosticPayload": {
            "additionalProperties": false,
            "properties": {
                "code": {
                    "type": "string"
                },
                "evidence_ref": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "message": {
                    "type": "string"
                },
                "severity": {
                    "enum": [
                        "info",
                        "warning",
                        "error"
                    ]
                }
            },
            "required": [
                "code",
                "message",
                "severity"
            ],
            "type": "object"
        },
        "TimelineSubjectPayload": {
            "additionalProperties": false,
            "properties": {
                "id": {
                    "type": "string"
                },
                "kind": {
                    "enum": [
                        "dashboard",
                        "issue",
                        "e2e_suite",
                        "e2e_run"
                    ]
                },
                "label": {
                    "type": "string"
                },
                "outcome": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "status": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },
            "required": [
                "kind",
                "id",
                "label"
            ],
            "type": "object"
        },
        "TimelineView": {
            "enum": [
                "user",
                "ops",
                "debug",
                "raw"
            ],
            "type": "string"
        },
        "UnownedRecoveryRecordPayload": {
            "additionalProperties": false,
            "properties": {
                "kind": {
                    "const": "unowned"
                },
                "work": {
                    "$ref": "#/components/schemas/RecoveryRecordFactPayload"
                }
            },
            "required": [
                "kind",
                "work"
            ],
            "type": "object"
        },
        "ValidationEvidenceMissingPayload": {
            "additionalProperties": false,
            "properties": {
                "diagnostics": {
                    "items": {
                        "$ref": "#/components/schemas/TimelineDiagnosticPayload"
                    },
                    "type": "array"
                },
                "expected_record_path": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "kind": {
                    "const": "missing_evidence"
                }
            },
            "required": [
                "kind",
                "diagnostics"
            ],
            "type": "object"
        },
        "ValidationExtraPayload": {
            "additionalProperties": false,
            "properties": {
                "namespace": {
                    "type": "string"
                },
                "payload": {
                    "additionalProperties": true,
                    "type": "object"
                }
            },
            "required": [
                "namespace",
                "payload"
            ],
            "type": "object"
        },
        "ValidationFailedPayload": {
            "additionalProperties": false,
            "properties": {
                "command": {
                    "type": "string"
                },
                "details_command": {
                    "$ref": "#/components/schemas/OpenValidationDetailsCommandPayload"
                },
                "failure_summary": {
                    "type": "string"
                },
                "kind": {
                    "const": "failed"
                },
                "record_path": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "command",
                "record_path",
                "failure_summary",
                "details_command"
            ],
            "type": "object"
        },
        "ValidationFailureActionSectionPayload": {
            "additionalProperties": false,
            "properties": {
                "actions": {
                    "items": {
                        "$ref": "#/components/schemas/DialogActionPayload"
                    },
                    "type": "array"
                },
                "title": {
                    "type": "string"
                }
            },
            "required": [
                "title",
                "actions"
            ],
            "type": "object"
        },
        "ValidationFailureDialogPayload": {
            "additionalProperties": false,
            "properties": {
                "action_sections": {
                    "items": {
                        "$ref": "#/components/schemas/ValidationFailureActionSectionPayload"
                    },
                    "type": "array"
                },
                "command": {
                    "type": "string"
                },
                "ended_at": {
                    "type": "string"
                },
                "exit_code": {
                    "type": [
                        "integer",
                        "null"
                    ]
                },
                "failed_tests": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "junit_cases": {
                    "items": {
                        "$ref": "#/components/schemas/JUnitCasePayload"
                    },
                    "type": "array"
                },
                "reason": {
                    "type": "string"
                },
                "started_at": {
                    "type": "string"
                },
                "status": {
                    "enum": [
                        "passed",
                        "failed"
                    ],
                    "type": "string"
                },
                "stderr_excerpt": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "stdout_excerpt": {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                },
                "suite": {
                    "type": "string"
                },
                "summary_rows": {
                    "items": {
                        "$ref": "#/components/schemas/DialogRowPayload"
                    },
                    "type": "array"
                },
                "title": {
                    "type": "string"
                }
            },
            "required": [
                "title",
                "status",
                "reason",
                "suite",
                "command",
                "exit_code",
                "started_at",
                "ended_at",
                "failed_tests",
                "stdout_excerpt",
                "stderr_excerpt",
                "junit_cases",
                "summary_rows",
                "action_sections"
            ],
            "type": "object"
        },
        "ValidationNotRunPayload": {
            "additionalProperties": false,
            "properties": {
                "kind": {
                    "const": "not_run"
                },
                "reason": {
                    "enum": [
                        "coding_in_progress",
                        "validation_disabled",
                        "not_required"
                    ]
                }
            },
            "required": [
                "kind",
                "reason"
            ],
            "type": "object"
        },
        "ValidationOutcomePayload": {
            "discriminator": {
                "propertyName": "kind"
            },
            "oneOf": [
                {
                    "$ref": "#/components/schemas/ValidationPassedPayload"
                },
                {
                    "$ref": "#/components/schemas/ValidationFailedPayload"
                },
                {
                    "$ref": "#/components/schemas/ValidationNotRunPayload"
                },
                {
                    "$ref": "#/components/schemas/ValidationEvidenceMissingPayload"
                }
            ]
        },
        "ValidationPassedPayload": {
            "additionalProperties": false,
            "properties": {
                "command": {
                    "type": "string"
                },
                "details_command": {
                    "$ref": "#/components/schemas/OpenValidationDetailsCommandPayload"
                },
                "kind": {
                    "const": "passed"
                },
                "record_path": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "command",
                "record_path",
                "details_command"
            ],
            "type": "object"
        },
        "ViewClaudeLogCommandPayload": {
            "additionalProperties": false,
            "properties": {
                "error_surface": {
                    "enum": [
                        "toast",
                        "inline"
                    ]
                },
                "issue_number": {
                    "type": "integer"
                },
                "kind": {
                    "const": "view_claude_log"
                },
                "label": {
                    "type": "string"
                },
                "run_dir": {
                    "type": "string"
                }
            },
            "required": [
                "kind",
                "label",
                "issue_number",
                "run_dir",
                "error_surface"
            ],
            "type": "object"
        },
        "ViewModelSnapshotPayload": {
            "additionalProperties": false,
            "properties": {
                "active_tab": {
                    "type": "string"
                },
                "count": {
                    "type": "integer"
                },
                "rows": {
                    "items": {
                        "$ref": "#/components/schemas/IssueRowPayload"
                    },
                    "type": "array"
                },
                "view_model": {
                    "$ref": "#/components/schemas/DashboardViewModelPayload"
                }
            },
            "required": [
                "view_model",
                "rows",
                "active_tab",
                "count"
            ],
            "type": "object"
        },
        "WorktreeAuditActivityEvidence": {
            "enum": [
                "known",
                "unknown"
            ],
            "type": "string"
        },
        "WorktreeAuditDisposition": {
            "enum": [
                "managed",
                "cleanup_candidate",
                "retained"
            ],
            "type": "string"
        },
        "WorktreeAuditEntryPayload": {
            "additionalProperties": false,
            "properties": {
                "disposition": {
                    "$ref": "#/components/schemas/WorktreeAuditDisposition"
                },
                "kind": {
                    "$ref": "#/components/schemas/WorktreeAuditKind"
                },
                "name": {
                    "minLength": 1,
                    "type": "string"
                },
                "path": {
                    "minLength": 1,
                    "type": "string"
                },
                "reason": {
                    "minLength": 1,
                    "type": "string"
                }
            },
            "required": [
                "path",
                "name",
                "kind",
                "disposition",
                "reason"
            ],
            "type": "object"
        },
        "WorktreeAuditKind": {
            "enum": [
                "issue",
                "reviewer",
                "tech_lead_scratch",
                "external"
            ],
            "type": "string"
        },
        "WorktreeAuditRequestPayload": {
            "additionalProperties": false,
            "properties": {
                "repo_root": {
                    "minLength": 1,
                    "type": "string"
                }
            },
            "required": [
                "repo_root"
            ],
            "type": "object"
        },
        "WorktreeAuditResponsePayload": {
            "additionalProperties": false,
            "properties": {
                "activity_evidence": {
                    "$ref": "#/components/schemas/WorktreeAuditActivityEvidence"
                },
                "audit_unavailable": {
                    "type": "boolean"
                },
                "cleanup_candidates": {
                    "items": {
                        "$ref": "#/components/schemas/WorktreeAuditEntryPayload"
                    },
                    "type": "array"
                },
                "issue_cleanup_enabled": {
                    "type": [
                        "boolean",
                        "null"
                    ]
                },
                "message": {
                    "minLength": 1,
                    "type": "string"
                },
                "note": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "scope": {
                    "$ref": "#/components/schemas/WorktreeAuditScope"
                },
                "stale_worktrees": {
                    "items": {
                        "$ref": "#/components/schemas/WorktreeAuditEntryPayload"
                    },
                    "type": "array"
                },
                "worktrees": {
                    "items": {
                        "$ref": "#/components/schemas/WorktreeAuditEntryPayload"
                    },
                    "type": "array"
                }
            },
            "required": [
                "worktrees",
                "cleanup_candidates",
                "stale_worktrees",
                "message",
                "issue_cleanup_enabled",
                "activity_evidence",
                "audit_unavailable",
                "scope",
                "note"
            ],
            "type": "object"
        },
        "WorktreeAuditScope": {
            "enum": [
                "configured",
                "repo-parent-fallback"
            ],
            "type": "string"
        }
    };

    const MAX_REF_HOPS = 100;

    function schemaNames() {
        return Object.keys(SCHEMAS).sort();
    }

    function hasSchema(name) {
        return Object.prototype.hasOwnProperty.call(SCHEMAS, name);
    }

    // Validates ``value`` against the named contract schema.
    //
    // Returns a result object rather than throwing, so callers fail
    // closed on ``ok === false`` without wrapping every JSON boundary in
    // a try/catch.  An unknown ``schemaName`` DOES throw: the name is
    // always a literal in browser code, never wire data, so a miss is a
    // programming error and must not be swallowed.
    function validate(schemaName, value) {
        if (!hasSchema(schemaName)) {
            throw new Error('Unknown UI contract schema: ' + schemaName);
        }
        const errors = [];
        _check(SCHEMAS[schemaName], value, '$', errors);
        if (errors.length) {
            return { ok: false, value: null, errors: errors, schemaName: schemaName };
        }
        return { ok: true, value: value, errors: [], schemaName: schemaName };
    }

    function _refName(ref) {
        const text = String(ref);
        return text.slice(text.lastIndexOf('/') + 1);
    }

    function _resolve(schema, path, errors) {
        let current = schema;
        let hops = 0;
        while (current && typeof current === 'object' && current.$ref) {
            const name = _refName(current.$ref);
            if (!hasSchema(name)) {
                errors.push(path + ': contract references unknown schema ' + name);
                return null;
            }
            current = SCHEMAS[name];
            hops += 1;
            if (hops > MAX_REF_HOPS) {
                errors.push(path + ': contract $ref chain does not terminate');
                return null;
            }
        }
        if (!current || typeof current !== 'object' || Array.isArray(current)) {
            errors.push(path + ': contract schema is not an object');
            return null;
        }
        return current;
    }

    function _check(schema, value, path, errors) {
        const resolved = _resolve(schema, path, errors);
        if (!resolved) return;
        if (resolved.nullable === true && value === null) return;
        if (Object.prototype.hasOwnProperty.call(resolved, 'const')) {
            if (value !== resolved.const) {
                errors.push(
                    path + ': expected ' + JSON.stringify(resolved.const)
                    + ', got ' + _describe(value),
                );
            }
            return;
        }
        if (Array.isArray(resolved.enum)) {
            if (resolved.enum.indexOf(value) === -1) {
                errors.push(
                    path + ': expected one of ' + JSON.stringify(resolved.enum)
                    + ', got ' + _describe(value),
                );
            }
            return;
        }
        const union = resolved.oneOf || resolved.anyOf;
        if (union) {
            _checkUnion(resolved, union, value, path, errors);
            return;
        }
        _checkTyped(resolved, value, path, errors);
    }

    function _checkTyped(schema, value, path, errors) {
        if (schema.type === undefined) return;
        const types = Array.isArray(schema.type) ? schema.type : [schema.type];
        let matched = null;
        for (const type of types) {
            if (_matchesType(type, value)) {
                matched = type;
                break;
            }
        }
        if (matched === null) {
            errors.push(path + ': expected ' + types.join(' | ') + ', got ' + _describe(value));
            return;
        }
        if (matched === 'object') {
            _checkObject(schema, value, path, errors);
        } else if (matched === 'array') {
            _checkArray(schema, value, path, errors);
        } else if (matched === 'string') {
            _checkString(schema, value, path, errors);
        } else if (matched === 'integer' || matched === 'number') {
            _checkNumber(schema, value, path, errors);
        }
    }

    // Wire-format types, so no coercion: "88" is not an integer and
    // `true` is not a 1.  This mirrors the ``strict=True`` the Python
    // generator emits for constrained integers — a malformed payload
    // must be rejected, never silently normalized.
    function _matchesType(type, value) {
        switch (type) {
            case 'null': return value === null;
            case 'boolean': return typeof value === 'boolean';
            case 'string': return typeof value === 'string';
            case 'integer': return typeof value === 'number' && Number.isInteger(value);
            case 'number': return typeof value === 'number' && Number.isFinite(value);
            case 'array': return Array.isArray(value);
            case 'object': return value !== null && typeof value === 'object' && !Array.isArray(value);
            default: return false;
        }
    }

    function _checkUnion(schema, branches, value, path, errors) {
        const propertyName = schema.discriminator && schema.discriminator.propertyName;
        if (propertyName && _matchesType('object', value)) {
            const branch = _branchForTag(branches, propertyName, value[propertyName]);
            if (!branch) {
                errors.push(
                    path + '.' + propertyName + ': no contract variant matches '
                    + _describe(value[propertyName]),
                );
                return;
            }
            _check(branch, value, path, errors);
            return;
        }
        // Untagged union, or a non-object under a discriminated union
        // (e.g. the `null` branch of an optional $ref): the value must
        // satisfy at least one variant.
        for (const branch of branches) {
            const branchErrors = [];
            _check(branch, value, path, branchErrors);
            if (!branchErrors.length) return;
        }
        errors.push(path + ': matches no contract variant, got ' + _describe(value));
    }

    function _branchForTag(branches, propertyName, tag) {
        for (const branch of branches) {
            const resolved = _resolve(branch, '$', []);
            if (!resolved) continue;
            const nested = resolved.oneOf || resolved.anyOf;
            if (nested) {
                const nestedBranch = _branchForTag(nested, propertyName, tag);
                if (nestedBranch) return nestedBranch;
                continue;
            }
            if (!resolved.properties) continue;
            const tagSchema = resolved.properties[propertyName];
            if (!tagSchema) continue;
            if (Object.prototype.hasOwnProperty.call(tagSchema, 'const')) {
                if (tagSchema.const === tag) return branch;
            } else if (Array.isArray(tagSchema.enum) && tagSchema.enum.indexOf(tag) !== -1) {
                return branch;
            }
        }
        return null;
    }

    function _checkObject(schema, value, path, errors) {
        const properties = schema.properties || {};
        const required = Array.isArray(schema.required) ? schema.required : [];
        for (const key of required) {
            if (!Object.prototype.hasOwnProperty.call(value, key)) {
                errors.push(path + '.' + key + ': required property is missing');
            }
        }
        const additional = schema.additionalProperties;
        for (const key of Object.keys(value)) {
            const childPath = path + '.' + key;
            if (Object.prototype.hasOwnProperty.call(properties, key)) {
                // Parity with the generated Pydantic models: a property
                // outside `required` renders as `T | None = None`, which
                // accepts an explicit null on the wire.
                if (value[key] === null && required.indexOf(key) === -1) continue;
                _check(properties[key], value[key], childPath, errors);
            } else if (additional === true) {
                continue;
            } else if (additional && typeof additional === 'object') {
                _check(additional, value[key], childPath, errors);
            } else {
                errors.push(childPath + ': unexpected property is not allowed by the contract');
            }
        }
    }

    function _checkArray(schema, value, path, errors) {
        if (schema.minItems !== undefined && value.length < schema.minItems) {
            errors.push(path + ': expected minItems ' + schema.minItems + ', got length ' + value.length);
        }
        if (schema.maxItems !== undefined && value.length > schema.maxItems) {
            errors.push(path + ': expected maxItems ' + schema.maxItems + ', got length ' + value.length);
        }
        if (schema.items === undefined) return;
        for (let index = 0; index < value.length; index += 1) {
            _check(schema.items, value[index], path + '[' + index + ']', errors);
        }
    }

    function _checkString(schema, value, path, errors) {
        if (schema.minLength !== undefined && value.length < schema.minLength) {
            errors.push(path + ': expected minLength ' + schema.minLength + ', got length ' + value.length);
        }
        if (schema.maxLength !== undefined && value.length > schema.maxLength) {
            errors.push(path + ': expected maxLength ' + schema.maxLength + ', got length ' + value.length);
        }
        if (schema.pattern !== undefined && !(new RegExp(schema.pattern)).test(value)) {
            errors.push(path + ': does not match pattern ' + JSON.stringify(schema.pattern));
        }
    }

    function _checkNumber(schema, value, path, errors) {
        if (schema.minimum !== undefined && value < schema.minimum) {
            errors.push(path + ': expected >= ' + schema.minimum + ', got ' + value);
        }
        if (schema.exclusiveMinimum !== undefined && value <= schema.exclusiveMinimum) {
            errors.push(path + ': expected > ' + schema.exclusiveMinimum + ', got ' + value);
        }
        if (schema.maximum !== undefined && value > schema.maximum) {
            errors.push(path + ': expected <= ' + schema.maximum + ', got ' + value);
        }
        if (schema.exclusiveMaximum !== undefined && value >= schema.exclusiveMaximum) {
            errors.push(path + ': expected < ' + schema.exclusiveMaximum + ', got ' + value);
        }
    }

    function _describe(value) {
        if (value === null) return 'null';
        if (value === undefined) return 'undefined';
        if (Array.isArray(value)) return 'array';
        const type = typeof value;
        if (type === 'string' || type === 'number' || type === 'boolean') {
            return type + ' ' + JSON.stringify(value);
        }
        return type;
    }

    return {
        SCHEMAS: SCHEMAS,
        hasSchema: hasSchema,
        schemaNames: schemaNames,
        validate: validate,
    };
});
