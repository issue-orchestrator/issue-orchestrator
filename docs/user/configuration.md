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

## Filtering

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `filtering.label` | string (optional) | `None` | Only process issues with this label (optional) |
| `filtering.milestones` | string | `` | Comma-separated list of milestones to process |
| `filtering.exclude_labels` | string | `` | Comma-separated labels to exclude |
| `filtering.fetch_limit` | integer | `100` | Max issues to fetch per API call |
| `filtering.max_to_start` | integer | `0` | Stop after starting N issues (0 = unlimited) |

## Review

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `review.enabled` | boolean | `False` | Enable automated code review workflow |
| `review.default` | string (optional) | `None` | Agent label for code reviews (e.g., agent:reviewer) |
| `review.max_rework_cycles` | integer | `2` | Max times to re-queue work agent before escalating |
| `review.triage_review_agent` | string (optional) | `None` | Agent for batch reviews (optional) |
| `review.triage_review_threshold` | integer | `0` | Trigger triage after N PRs (0 = manual only) |

## Hooks

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `hooks.safety_check.interval_days` | integer | `7` | Run live hook verification every N days (0 = disabled) |
| `hooks.safety_check.dangerous_allow_failure` | boolean | `False` | If true, warn only on safety check failure; if false, block orchestrator start |

## Advanced

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `observability.session_no_output_seconds` | integer | `120` | Emit event after this much idle time |
| `observability.stale_escalation_ticks` | integer | `0` | Escalate after K consecutive stale ticks (0 = disabled) |
| `ui.web_port` | integer | `8080` |  |
| `ui.control_api_port` | integer | `19080` | 0 = disabled |
| `worktrees.base` | string | `../` | Directory where git worktrees are created |
| `worktrees.worktree_branch_on_recreate` | string | `delete` | What to do when recreating a worktree with existing branch |
