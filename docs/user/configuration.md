# Settings Reference

_Auto-generated from settings schema._

## Concurrency

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `execution.concurrency.max_concurrent_sessions` | integer | `3` | Maximum parallel agent sessions |
| `execution.concurrency.session_timeout_minutes` | integer | `45` | Kill sessions after this duration |
| `ui.queue_refresh_seconds` | integer | `600` | How often to refresh the issue queue from GitHub (0 = manual only) |

## E2E Runner

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `e2e.enabled` | boolean | `False` | Automatically run E2E tests when main branch changes |
| `e2e.auto_run_interval_minutes` | integer | `30` | Min interval between auto runs (0 = disable) |
| `e2e.role` | string | `auto` | Role in multi-orchestrator setup |
| `e2e.pytest_args` | string | `tests/e2e -v` | Space-separated pytest arguments (e.g., tests/e2e -v) |
| `e2e.allow_retry_once` | boolean | `True` | Retry failing tests to reduce flakiness |
| `e2e.stop_on_first_failure` | boolean | `False` | Add -x flag to stop test run on first failure |
| `e2e.quarantine_file` | string | `tests/e2e/quarantine.txt` | Path to quarantine file for skipping known-flaky tests |
| `e2e.auto_quarantine` | boolean | `True` | Automatically add failing tests to the quarantine list |
| `e2e.auto_create_issues` | boolean | `True` | Automatically create GitHub issues for failed tests |
| `e2e.issue_agent_label` | string | `agent:backend` | Agent label assigned to auto-created failure issues |

## Filtering

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `filtering.label` | string (optional) | `None` | Only process issues with this label (optional) |
| `filtering.milestones` | string | `` | Comma-separated list of milestones to process |
| `filtering.exclude_labels` | string | `` | Comma-separated labels to exclude |
| `filtering.fetch_limit` | integer | `100` | Max issues to fetch per API call |
| `filtering.max_to_start` | integer | `0` | Stop after starting N issues (0 = unlimited) |

## Milestones

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `milestones.order` | string | `` | Explicit ordered list of milestone titles. Does not filter; unlisted milestones are appended using the milestone sort strategy. |

## Review

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `review.enabled` | boolean | `False` | Enable automated code review workflow |
| `review.default` | string (optional) | `None` | Agent label for code reviews (e.g., agent:reviewer) |
| `review.max_rework_cycles` | integer | `2` | Max times to re-queue work agent before escalating |
| `review.keep_current_approach_label` | string | `reviewer-keep-current-approach` | Label that tells reviewer to avoid alternative approaches |
| `review.exchange.mode` | string | `via-draft-pr` | Review exchange mode (via-mcp loop, local loop, or via-draft-pr review) |
| `review.exchange.probe.schedule` | string | `daily` | When to run MCP round-trip validation |
| `review.exchange.probe.interval_days` | integer | `1` | Interval for MCP round-trip validation when schedule=interval |
| `review.exchange.loop.max_rounds` | integer | `10` | Max coder/reviewer rounds before stopping the MCP loop |
| `review.exchange.loop.max_no_progress` | integer | `2` | Max rounds where reviewer reports no progress before stopping |
| `review.exchange.loop.require_validation` | boolean | `True` | Require a validation record before reviewer can approve |
| `review.triage_review_agent` | string (optional) | `None` | Agent for batch reviews (optional) |
| `review.triage_review_threshold` | integer | `0` | Trigger triage after N PRs (0 = manual only) |

## Goal Pilot

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `goal_pilot.enabled` | boolean | `False` | Enable the Goal Pilot AI controller |
| `goal_pilot.agent` | string (optional) | `None` | Agent label to run as Goal Pilot (e.g., agent:goal-pilot) |
| `goal_pilot.approval_policy` | string | `journeys_only` | How Goal Pilot applies repo changes |
| `goal_pilot.approval_batch_size` | integer | `10` | How many changes to bundle before approval (batch mode) |
| `goal_pilot.approval_batch_window_minutes` | integer | `60` | Max time to wait before asking for approval (batch mode) |

## Hooks

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `hooks.safety_check.interval_days` | integer | `7` | Run live hook verification every N days (0 = disabled) |
| `hooks.safety_check.dangerous_allow_failure` | boolean | `False` | If true, warn only on safety check failure; if false, block orchestrator start |

## Advanced

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `sqlite_backup.enabled` | boolean | `True` | Enable automatic backups of local SQLite state |
| `sqlite_backup.cadence_hours` | integer | `24` | Minimum hours between backups |
| `sqlite_backup.check_interval_minutes` | integer | `60` | How often to check whether backups are due |
| `sqlite_backup.retention_daily` | integer | `14` | Number of daily backups to keep |
| `sqlite_backup.retention_weekly` | integer | `8` | Number of weekly backups to keep |
| `sqlite_backup.enforce_on_startup` | boolean | `True` | If cadence elapsed, force a backup on startup |
| `observability.session_no_output_seconds` | integer | `120` | Emit event after this much idle time |
| `observability.stale_escalation_ticks` | integer | `0` | Escalate after K consecutive stale ticks (0 = disabled) |
| `ui.web_port` | integer | `8080` |  |
| `ui.control_api_port` | integer | `19080` | 0 = disabled |
| `ai_systems.allowed` | string | `` | Additional ai_system values allowed in config (comma-separated) |
| `worktrees.base` | string | `../` | Directory where git worktrees are created |
| `worktrees.worktree_branch_on_recreate` | string | `delete` | What to do when recreating a worktree with existing branch |
