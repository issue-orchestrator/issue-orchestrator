# Configuration Reference

This is the full configuration reference auto-generated from the settings schema.
For a short onboarding guide, see `docs/user/configuration.md`.

<!-- BEGIN AUTO-GENERATED CONFIG REFERENCE — regenerate via: pytest tests/unit/test_settings_schema.py::TestDriftDetection::test_config_reference_not_stale -->

# Settings Reference

_Auto-generated from settings schema._

## Concurrency

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `execution.concurrency.max_concurrent_sessions` | integer | `3` | Maximum parallel agent sessions | `1`, `3`, `5` | Set based on CPU, RAM, and how many concurrent sessions you can actively review. |
| `execution.concurrency.session_timeout_minutes` | integer | `45` | Kill sessions after this duration | `30`, `45`, `90` | Lower values fail faster for stuck sessions; higher values help long builds. |
| `ui.queue_refresh_seconds` | integer | `600` | How often to refresh the issue queue from GitHub (0 = manual only) | `0`, `300`, `600` | Use 0 to disable automatic refreshes and refresh manually in the UI. |
| `ui.fetch_layer.enabled` | boolean | `True` | Enable incremental refreshes between periodic full scans | `true`, `false` | Disable to force a full GitHub queue scan on every refresh. |
| `ui.fetch_layer.network_sync_seconds` | integer | `60` | How often to run GitHub network sync cycles (independent of control tick) | `15`, `60`, `120` | Lower values improve freshness; higher values reduce GitHub API calls. |
| `ui.fetch_layer.full_scan_interval_seconds` | integer | `1800` | Run a full queue scan at this interval even when incremental mode is enabled | `600`, `1800`, `3600` | Lower values discover new work faster; higher values reduce API usage. |
| `ui.fetch_layer.discovery_limit` | integer | `25` | Max issues fetched per incremental discovery pass | `0`, `25`, `50` | Set to 0 to disable discovery during incremental refreshes. |
| `ui.fetch_layer.max_hot_issues_per_cycle` | integer | `40` | Max existing queue issues to refresh by direct issue lookup per cycle | `20`, `40`, `100` | Higher values improve freshness but increase API usage. |
| `ui.fetch_layer.pr_scan_every_n_refreshes` | integer | `2` | Scan review/rework PRs every N queue refreshes | `1`, `2`, `3` | Use 1 for max freshness; increase to reduce PR API calls. |
| `ui.fetch_layer.dependency_scan_every_n_refreshes` | integer | `1` | Recompute dependency blocking every N queue refreshes | `1`, `2`, `3` | Use 1 for immediate dependency updates; increase to reduce load. |
| `ui.fetch_layer.visibility_aware_enabled` | boolean | `False` | Prioritize refresh for issues currently visible in the Flow board | `true`, `false` | Requires browser visibility hints from the Flow board. |
| `ui.fetch_layer.selective_sync_planner_enabled` | boolean | `False` | Enable cross-entity selective sync planning for queue refresh cycles | `true`, `false` | Use with telemetry to tune freshness versus API cost. |
| `scheduling.default_priority_tier` | integer | `1` | Default priority tier when none is specified (0-9) | `0`, `1`, `2` | Used when issue titles do not include a [P?-nnn] prefix. |

## E2E Runner

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `e2e.enabled` | boolean | `False` | Automatically run E2E tests when main branch changes | `true`, `false` | Keep disabled on repos without stable E2E tests. |
| `e2e.auto_run_interval_minutes` | integer | `30` | Min interval between auto runs (0 = disable) | `0`, `30`, `60` | Set to 0 to disable automatic runs and trigger manually. |
| `e2e.role` | string | `auto` | Role in multi-orchestrator setup | `auto`, `executor`, `reader`, `disabled` | Use executor on the single machine that should run tests. |
| `e2e.pytest_args` | string | `tests/e2e -v` | Space-separated pytest arguments (e.g., tests/e2e -v) | `tests/e2e -v`, `tests/e2e -v -x` | First argument should be a path; it is validated by the doctor. |
| `e2e.allow_retry_once` | boolean | `True` | Retry failing tests to reduce flakiness | `true`, `false` | Disable if reruns hide real failures or are too slow. |
| `e2e.stop_on_first_failure` | boolean | `False` | Add -x flag to stop test run on first failure | `true`, `false` | Enable for faster feedback when most tests pass. |
| `e2e.quarantine_file` | string | `tests/e2e/quarantine.txt` | Path to quarantine file for skipping known-flaky tests | `tests/e2e/quarantine.txt`, `tests/e2e/quarantine-local.txt` | Doctor verifies the file exists when E2E is enabled. |
| `e2e.auto_quarantine` | boolean | `True` | Automatically add failing tests to the quarantine list | `true`, `false` | Set false to require manual quarantine updates. |
| `e2e.auto_create_issues` | boolean | `True` | Automatically create GitHub issues for failed tests | `true`, `false` | Disable if you prefer manual triage of failures. |
| `e2e.issue_agent_label` | string | `agent:backend` | Agent label assigned to auto-created failure issues | `agent:backend`, `agent:triage` | Must refer to an agent defined in the config. |

## Filtering

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `filtering.label` | string (optional) | `None` | Only process issues with this label (optional) | `bot-ready`, `needs-triage` | Use a single label to gate which issues are eligible. |
| `filtering.milestones` | string | `` | Milestones to process (comma-separated string or YAML list) | `M1, M2`, `["M1", "M2"]` | Accepts a comma-separated string or a YAML list. Leave empty to allow all milestones. |
| `filtering.exclude_labels` | string | `` | Labels to exclude (comma-separated string or YAML list) | `test-data, skip`, `["test-data", "skip"]` | Accepts a comma-separated string or a YAML list. |
| `filtering.fetch_limit` | integer | `100` | Max issues to fetch per API call | `50`, `100`, `200` | Lower values reduce API load; higher values reduce pagination. |
| `filtering.max_to_start` | integer | `0` | Stop after starting N issues (0 = unlimited) | `0`, `5`, `10` | Useful for dry runs or throttling initial ramp-up. |

## Milestones

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `milestones.order` | string | `` | Explicit ordered list of milestone titles. Does not filter; unlisted milestones are appended using the milestone sort strategy. | `M1, M2` | Use to override the default sort order without filtering. |

## Review

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `review.enabled` | boolean | `False` | Enable automated code review workflow | `true`, `false` | When enabled, a reviewer agent validates work agent PRs. |
| `review.default` | string (optional) | `None` | Agent label for code reviews (e.g., agent:reviewer) | `agent:reviewer` | Must match a label defined under agents. |
| `review.max_rework_cycles` | integer | `10` | Max times to re-queue work agent before escalating | `0`, `2`, `10` | Set to 0 to disable rework cycles (immediate escalation). |
| `review.keep_current_approach_label` | string | `reviewer-keep-current-approach` | Label that tells reviewer to avoid alternative approaches | `reviewer-keep-current-approach` | Applied to issues where stability is preferred over refactors. |
| `review.exchange.mode` | string | `via-draft-pr` | Review exchange mode (via-mcp loop, local loop, or via-draft-pr review) | `via-draft-pr`, `via-mcp`, `auto` | Draft PR mode is the default and requires no extra setup. |
| `review.exchange.probe.schedule` | string | `daily` | When to run MCP round-trip validation | `daily`, `startup`, `interval`, `manual` | Use manual to disable automatic probes and run on demand. |
| `review.exchange.probe.interval_days` | integer | `1` | Interval for MCP round-trip validation when schedule=interval | `1`, `7`, `14` | Used only when schedule=interval. |
| `review.exchange.loop.max_rounds` | integer | `10` | Max coder/reviewer rounds before stopping the MCP loop | `5`, `10`, `20` | Higher values allow longer back-and-forth reviews. |
| `review.exchange.loop.max_no_progress` | integer | `2` | Max rounds where reviewer reports no progress before stopping | `1`, `2`, `3` | Limits loops when reviewer is not seeing improvements. |
| `review.exchange.loop.require_validation` | boolean | `True` | Require a validation record before reviewer can approve | `true`, `false` | Disable only if you accept reviewer approvals without validation. |
| `review.triage_review_agent` | string (optional) | `None` | Agent for batch reviews (optional) | `agent:triage` | Must match a label defined under agents. |
| `review.triage_review_threshold` | integer | `0` | Trigger triage after N PRs (0 = manual only) | `0`, `5`, `10` | Set to 0 to only trigger triage manually. |

## Goal Pilot

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `goal_pilot.enabled` | boolean | `False` | Enable the Goal Pilot AI controller | `true`, `false` | Enable only when Goal Pilot prompts are configured and tested. |
| `goal_pilot.agent` | string (optional) | `None` | Agent label to run as Goal Pilot (e.g., agent:goal-pilot) | `agent:goal-pilot` | Must match a label defined under agents. |
| `goal_pilot.approval_policy` | string | `journeys_only` | How Goal Pilot applies repo changes | `journeys_only`, `gatekeeper`, `batch` | Batch mode bundles changes before approval; gatekeeper requests approval per change. |
| `goal_pilot.approval_batch_size` | integer | `10` | How many changes to bundle before approval (batch mode) | `5`, `10`, `25` | Used only when approval_policy=batch. |
| `goal_pilot.approval_batch_window_minutes` | integer | `60` | Max time to wait before asking for approval (batch mode) | `30`, `60`, `120` | Used only when approval_policy=batch. |

## Hooks

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `hooks.ai_gate.interval_days` | integer | `7` | Run AI gate tests every N days (0 = disabled) | `0`, `7`, `30` | Set to 0 to disable periodic AI gate tests. |
| `hooks.ai_gate.dangerous_allow_failure` | boolean | `False` | If true, warn only on AI gate failure; if false, block orchestrator start | `true`, `false` | Keep false in production to enforce hook integrity. |

## Advanced

| Field | Type | Default | Description | Examples | Notes |
|-------|------|---------|-------------|----------|-------|
| `sqlite_backup.enabled` | boolean | `True` | Enable automatic backups of local SQLite state | `true`, `false` | Disable only if backups are managed externally. |
| `sqlite_backup.cadence_hours` | integer | `24` | Minimum hours between backups | `6`, `24`, `48` | Lower values increase backup frequency. |
| `sqlite_backup.check_interval_minutes` | integer | `60` | How often to check whether backups are due | `30`, `60`, `120` | Checks are lightweight; keep reasonably frequent. |
| `sqlite_backup.retention_daily` | integer | `14` | Number of daily backups to keep | `7`, `14`, `30` | Set to 0 to disable daily backups. |
| `sqlite_backup.retention_weekly` | integer | `8` | Number of weekly backups to keep | `4`, `8`, `12` | Set to 0 to disable weekly backups. |
| `sqlite_backup.enforce_on_startup` | boolean | `True` | If cadence elapsed, force a backup on startup | `true`, `false` | Keeps backups current if the process was stopped for a while. |
| `timeline.max_records` | integer | `5000` | Max timeline events kept per issue before trimming | `2000`, `5000`, `10000` | Set to 0 to disable trimming; higher values keep more history but grow state files faster. |
| `provider_resilience.short_retry.max_attempts` | integer | `4` | Max attempts for transient provider failures | `2`, `4`, `6` | Higher values reduce failures but can prolong degraded runs. |
| `provider_resilience.short_retry.initial_backoff_seconds` | integer | `5` | Initial backoff for transient provider retries | `2`, `5`, `10` | Shorter backoffs retry faster but can amplify rate limits. |
| `provider_resilience.short_retry.max_backoff_seconds` | integer | `60` | Maximum backoff for transient provider retries | `30`, `60`, `120` | Caps exponential backoff to avoid excessive waiting. |
| `provider_resilience.short_retry.jitter` | boolean | `True` | Apply full jitter to provider retry backoff | `true`, `false` | Keep enabled to avoid synchronized retry storms. |
| `provider_resilience.circuit_breaker.cooldown_seconds` | integer | `1800` | Cooldown window before retrying provider after outage | `600`, `1800`, `3600` | Longer cooldowns reduce repeated failures during incidents. |
| `provider_resilience.circuit_breaker.max_cooldowns` | integer | `6` | Maximum cooldown escalation steps | `3`, `6`, `8` | Limits how long we will keep extending cooldowns. |
| `provider_resilience.circuit_breaker.label` | string | `blocked:provider-unavailable` | Label applied when provider is unavailable | `blocked:provider-unavailable` | Use a label that is visible and searchable in your workflow. |
| `observability.session_no_output_seconds` | integer | `120` | Emit event after this much idle time | `60`, `120`, `300` | Lower values surface silent sessions sooner. |
| `observability.stale_escalation_ticks` | integer | `0` | Escalate after K consecutive stale ticks (0 = disabled) | `0`, `3`, `5` | Set to 0 to disable automatic escalation. |
| `observability.session_output_retention_days` | integer | `7` | Retention window in days for session run artifacts | `0`, `7`, `30` | Set to 0 to expire immediately; cleanup policy may still defer deletion. |
| `observability.session_output_retention_tier` | string | `hot` | Retention tier tag recorded in run manifests | `hot`, `cold` | Use hot for short-term troubleshooting and cold for longer forensic retention. |
| `ui.web_port` | integer | `8080` | Port for the web dashboard (requires restart) | `8080`, `3000`, `9090` | Change if the default port is occupied. |
| `ui.control_api_port` | integer | `19080` | 0 = disabled | `0`, `19080`, `19081` | Set to 0 to disable the control API listener. |
| `ai_systems.allowed` | string | `` | Additional ai_system values allowed in config (comma-separated) | `codex, custom-system` | Use to allow new providers beyond ai_systems.yaml. |
| `worktrees.base` | string | `../` | Directory where git worktrees are created | `../`, `../worktrees`, `/tmp/worktrees` | Relative paths are resolved from the repo root. |
| `worktrees.base_branch_override` | string (optional) | `None` | Override the base branch for worktree creation (auto-detect if unset) | `main`, `master` | Use when your default branch is not auto-detected correctly. |
| `worktrees.worktree_branch_on_recreate` | string | `delete` | What to do when recreating a worktree with existing branch | `delete`, `create_new_branch` | Use create_new_branch to keep the old branch intact. |

<!-- END AUTO-GENERATED CONFIG REFERENCE -->
