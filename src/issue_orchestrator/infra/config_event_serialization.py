"""Runtime event projection for the merged orchestrator configuration.

The event payload is intentionally complete rather than minimal: it records the
effective YAML and command-line settings used by a running repository engine.
Keeping this projection outside ``Config`` gives the large configuration model a
single serialization boundary while preserving the existing public payload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import github_config as _github_config
from .budgeted_validation_config import serialize_budgeted_validation
from .config_review_projection import (
    internal_review_dict,
    runtime_exchange_dict,
    runtime_run_audit_dict,
)

if TYPE_CHECKING:
    from .config import Config


def config_event_dict(config: "Config") -> dict:
    """Return the complete effective configuration for trace events."""
    result = {
        "launch_selection": config.launch_identity_dict(),
        "repo": {
            "name": config.repo,
            "root": str(config.repo_root),
            "github": {
                **_github_config.github_auth_event_fields(config),
                "write_verify": {
                    "timeout_seconds": config.gh_write_verify_timeout_seconds,
                    "initial_delay_ms": config.gh_write_verify_initial_delay_ms,
                    "max_delay_ms": config.gh_write_verify_max_delay_ms,
                    "backoff": config.gh_write_verify_backoff,
                    "jitter_ms": config.gh_write_verify_jitter_ms,
                },
                "rate_limit": {
                    "startup": config.gh_rate_limit_startup,
                    "every_calls": config.gh_rate_limit_every_calls,
                    "warn_fraction": config.gh_rate_limit_warn_fraction,
                    "warn_remaining": config.gh_rate_limit_warn_remaining,
                },
                "audit": {
                    "enabled": config.gh_audit_enabled,
                    "events": config.gh_audit_events,
                    "file": config.gh_audit_file,
                },
            },
        },
        "config": {
            "path": str(config.config_path) if config.config_path else None,
        },
        "state": {
            "file": str(config.state_file),
        },
        "worktrees": {
            "base": str(config.worktree_base),
            "base_branch_override": config.worktree_base_branch_override,
            "seed_ref": config.worktree_seed_ref,
            "setup": list(config.setup_worktree),
            "reuse_push_preflight": config.reuse_push_preflight,
            "allow_no_verify_dry_run_preflight": config.allow_no_verify_dry_run_preflight,
            "worktree_branch_on_recreate": config.worktree_branch_on_recreate,
            "remediation": {
                "pr_collision": config.worktree_remediation_pr_collision,
                "push_rebase_retry": config.worktree_remediation_push_rebase_retry,
            },
        },
        "execution": {
            "concurrency": {
                "max_concurrent_sessions": config.max_concurrent_sessions,
                "session_timeout_minutes": config.session_timeout_minutes,
            },
            "terminal_adapter": config.terminal_adapter,
            "isolation": {
                "mode": config.isolation.mode,
            },
        },
        "ui": {
            "mode": config.ui_mode,
            "web_port": config.web_port,
            "control_api_port": config.control_api_port,
            "queue_refresh_seconds": config.queue_refresh_seconds,
            "fetch_layer": {
                "enabled": config.fetch_layer_enabled,
                "network_sync_seconds": config.fetch_layer_network_sync_seconds,
                "full_scan_interval_seconds": config.fetch_layer_full_scan_interval_seconds,
                "discovery_limit": config.fetch_layer_discovery_limit,
                "max_hot_issues_per_cycle": config.fetch_layer_max_hot_issues_per_cycle,
                "pr_scan_every_n_refreshes": config.fetch_layer_pr_scan_every_n_refreshes,
                "dependency_scan_every_n_refreshes": config.fetch_layer_dependency_scan_every_n_refreshes,
                "visibility_aware_enabled": config.fetch_layer_visibility_aware_enabled,
                "selective_sync_planner_enabled": config.fetch_layer_selective_sync_planner_enabled,
            },
            "instances": config.instances,
            "flow_refresh": {
                "enabled": config.flow_refresh_enabled,
                "stale_seconds": config.flow_refresh_stale_seconds,
                "cooldown_seconds": config.flow_refresh_cooldown_seconds,
                "freshness_mode": config.flow_freshness_mode,
                "api_budget": config.flow_api_budget,
                "attention_priority": config.flow_attention_priority,
            },
        },
        "observability": {
            "session_no_output_seconds": config.session_no_output_seconds,
            "session_no_output_tail_lines": config.session_no_output_tail_lines,
            "session_no_output_max_bytes": config.session_no_output_max_bytes,
            "session_no_output_repeat_seconds": config.session_no_output_repeat_seconds,
            "session_output_retention_runs": config.session_output_retention_runs,
            "session_output_retention_days": config.session_output_retention_days,
            "session_output_retention_tier": config.session_output_retention_tier,
            "stale_escalation_ticks": config.stale_escalation_ticks,
            "comment_headings": {
                "implementation": config.comment_headings.implementation,
                "problems": config.comment_headings.problems,
                "pr_link": config.comment_headings.pr_link,
                "blocked": config.comment_headings.blocked,
                "needs_human": config.comment_headings.needs_human,
            },
        },
        "security": {
            "enforce_hooks": config.enforce_hooks,
            "pre_push_hook": str(config.pre_push_hook)
            if config.pre_push_hook
            else None,
            "dangerous": {
                "allow_unsupported_agents": config.dangerous.allow_unsupported_agents,
            },
        },
        "labels": {
            "in_progress": config.get_label_in_progress(),
            "blocked": config.get_label_blocked(),
            "needs_human": config.get_label_needs_human(),
            "needs_rework": config.get_label_needs_rework(),
            "validation_failed": config.get_label_validation_failed(),
            "prefix": config.label_prefix,
        },
        "validation": {
            "enabled": config.is_validation_enabled(),
            "budgeted": serialize_budgeted_validation(config.validation.budgeted),
            "quick": {
                "cmd": config.validation.quick.cmd,
                "timeout_seconds": config.validation.quick.timeout_seconds,
            },
            "publish": {
                "cmd": config.validation.publish.cmd,
                "timeout_seconds": config.validation.publish.timeout_seconds,
                "dirty_check": config.validation.publish.dirty_check,
            },
            "coverage_guardrail": {
                "enabled": config.validation.coverage_guardrail.enabled,
                "min_percent": config.validation.coverage_guardrail.min_percent,
                "apply_to": config.validation.coverage_guardrail.apply_to,
                "scope": config.validation.coverage_guardrail.scope,
                "coverage_type": config.validation.coverage_guardrail.coverage_type,
                "exclude": config.validation.coverage_guardrail.exclude,
            },
            "junit_xml_paths": list(config.validation.junit_xml_paths),
        },
        "review": {
            "enabled": config.review_enabled,
            "default": config.code_review_agent,
            "code_review_label": config.code_review_label,
            "code_reviewed_label": config.code_reviewed_label,
            "run_audit": runtime_run_audit_dict(config),
            "retrospective": {
                "enabled": config.retrospective_review_enabled,
                "trigger_label": config.retrospective_review_trigger_label,
                "reviewed_label": config.retrospective_reviewed_label,
                "changes_requested_label": config.retrospective_changes_requested_label,
            },
            "internal": internal_review_dict(config),
            "exchange": runtime_exchange_dict(config),
            "nits": {
                "default_policy": config.review_nits_default_policy,
                "by_agent": dict(config.review_nits_by_agent),
            },
            "tech_lead_review": {
                "agent": config.tech_lead_review_agent,
                "label": config.tech_lead_review_label,
                "reviewed_label": config.tech_lead_reviewed_label,
                "threshold": config.tech_lead_review_threshold,
                "on_failure": config.tech_lead_review_on_failure,
            },
            "max_rework_cycles": config.max_rework_cycles,
            "max_consecutive_publish_failures": config.max_consecutive_publish_failures,
            "max_consecutive_review_exchange_failures": config.max_consecutive_review_exchange_failures,
            "reviewer_feedback_cache_minutes": config.reviewer_feedback_cache_minutes,
        },
        "cleanup": {
            "with_tech_lead": {
                "close_ai_session_tabs": config.cleanup.with_tech_lead.close_ai_session_tabs,
                "remove_worktrees": config.cleanup.with_tech_lead.remove_worktrees,
            },
            "without_tech_lead": {
                "wait_for_code_review": config.cleanup.without_tech_lead.wait_for_code_review,
                "close_ai_session_tabs": config.cleanup.without_tech_lead.close_ai_session_tabs,
                "remove_worktrees": config.cleanup.without_tech_lead.remove_worktrees,
            },
        },
        "milestones": {
            "sort": config.milestone_sort,
            "sort_config": config.milestone_sort_config,
            "order": list(config.milestone_order),
            "foundation": config.foundation_milestone,
        },
        "filtering": {
            "label": config.filtering.label,
            "milestone": config.filtering.milestone,
            "milestones": list(config.filtering.milestones),
            "issue": config.filtering.issue,
            "exclude_labels": list(config.filtering.exclude_labels),
            "exclude_label_prefixes": list(config.filtering.exclude_label_prefixes),
            "fetch_limit": config.filtering.fetch_limit,
            "max_to_start": config.filtering.max_to_start,
        },
        "tech_lead": config.tech_lead.to_event_dict(enabled=config.tech_lead_enabled),
        "scheduling": {
            "default_priority_tier": config.scheduling.default_priority_tier,
        },
        "e2e": {
            "pr_labels": config.e2e_pr_labels,
            "enabled": config.e2e.enabled,
            "auto_run_interval_minutes": config.e2e.auto_run_interval_minutes,
            "runner_kind": config.e2e.runner_kind,
            "pytest_args": list(config.e2e.pytest_args),
            "command": list(config.e2e.command),
            "junit_xml_paths": list(config.e2e.junit_xml_paths),
            "artifact_paths": list(config.e2e.artifact_paths),
            "allow_retry_once": config.e2e.allow_retry_once,
            "quarantine_file": config.e2e.quarantine_file,
            "survive_restart": config.e2e.survive_restart,
            "auto_quarantine": config.e2e.auto_quarantine,
            "auto_create_issues": config.e2e.auto_create_issues,
            "issue_agent_label": config.e2e.issue_agent_label,
        },
        "sqlite_backup": {
            "enabled": config.sqlite_backup.enabled,
            "cadence_hours": config.sqlite_backup.cadence_hours,
            "check_interval_minutes": config.sqlite_backup.check_interval_minutes,
            "retention_daily": config.sqlite_backup.retention_daily,
            "retention_weekly": config.sqlite_backup.retention_weekly,
            "enforce_on_startup": config.sqlite_backup.enforce_on_startup,
        },
        "claims": {
            "enabled": config.claims.enabled,
            "claimant_id": config.claims.claimant_id,
            "lease_seconds": config.claims.lease_seconds,
            "renew_before_expiry_seconds": config.claims.renew_before_expiry_seconds,
            "convergence_timeout_seconds": config.claims.convergence_timeout_seconds,
            "convergence_poll_min_ms": config.claims.convergence_poll_min_ms,
            "convergence_poll_max_ms": config.claims.convergence_poll_max_ms,
        },
        "hooks": {
            "ai_gate": {
                "interval_days": config.hooks.ai_gate.interval_days,
                "dangerous_allow_failure": config.hooks.ai_gate.dangerous_allow_failure,
            },
        },
        "validated_work": {
            "escrow_retention_days": config.validated_work.escrow_retention_days
        },
        "merge_queue": {
            "enabled": config.merge_queue.enabled,
            "provider": config.merge_queue.provider,
            "enqueue_after": config.merge_queue.enqueue_after,
            "failure_action": config.merge_queue.failure_action,
        },
        "agents": {
            label: {
                "prompt_path": str(cfg.prompt_path),
                "model": cfg.model,
                "timeout_minutes": cfg.timeout_minutes,
                "meta_agent": cfg.meta_agent,
            }
            for label, cfg in config.agents.items()
        },
    }
    if config.session_interactions.enabled:
        result["execution"]["session_interactions"] = {"enabled": True}
    return result
