# Configuration

Configuration lives in `.issue-orchestrator/config/default.yaml` (or a named config like `main.yaml`).

---

## TL;DR - Minimal Config to Get Started

```yaml
agents:
  "agent:dev":
    prompt: ".issue-orchestrator/prompts/dev.md"
    model: "sonnet"

validation:
  cmd: "make test"
  timeout_seconds: 300
```

Label an issue with `agent:dev` and start the orchestrator.

### Quick additions

**Limit concurrency:**
```yaml
execution:
  concurrency:
    max_concurrent_sessions: 2
```

**Only process specific issues:**
```yaml
filtering:
  label: "bot-ready"
  milestone: "M1"
  milestones: ["M1", "M2"]
  issue: 123
  exclude_labels: ["test-data"]
```

**Enable code review:**
```yaml
review:
  enabled: true
  default: "agent:reviewer"

agents:
  "agent:reviewer":
    prompt: ".issue-orchestrator/prompts/reviewer.md"
    model: "sonnet"
```

---

## Reference (defaults shown)

### Environment variable substitution

Any string value in config can reference environment variables using `${VAR}` syntax:

```yaml
claims:
  claimant_id: "${ORCHESTRATOR_ID}"    # Expands to value of ORCHESTRATOR_ID env var

repo:
  github:
    token_env: "${GITHUB_TOKEN_VAR}"   # Works in any string field
```

If the referenced environment variable is not set, config loading fails with a clear error message showing which variable is missing and where it was referenced.

### repo

```yaml
repo:
  name: null
  root: null
  github:
    token: null
    token_env: null
    api_url: "https://api.github.com"
    http_timeout_seconds: 20
    cache_ttl_seconds: 300
    required_scopes: []
    allowed_scopes: []
    write_verify:
      timeout_seconds: 20
      initial_delay_ms: 250
      max_delay_ms: 2000
      backoff: 1.5
      jitter_ms: 0
    rate_limit:
      startup: true
      every_calls: 500
      warn_fraction: 0.1
      warn_remaining: 100
    audit:
      enabled: false
      events: false
      file: null
```

- `repo.name`: override repo if not detected from git remote.
- `repo.root`: override repo root path.

### worktrees

```yaml
worktrees:
  base: "../"
  setup:
    - "make install-vscode-extensions"
  reuse_push_preflight: true
  allow_no_verify_dry_run_preflight: true
  default_branch: "main"
  worktree_branch_on_recreate: "delete"
  remediation:
    pr_collision: "new_branch"
    push_rebase_retry: true
```

### execution

```yaml
execution:
  concurrency:
    max_concurrent_sessions: 3
    session_timeout_minutes: 45
  terminal_adapter: null  # "subprocess" or custom adapter path
  isolation:
    mode: "standard"
```

### initial_prompt

Template variables available:

| Variable | Description |
|----------|-------------|
| `{issue_number}` | GitHub issue number |
| `{issue_title}` | Issue title |
| `{prompt}` | Path to prompt file |
| `{worktree}` | Path to worktree |
| `{model}` | Model name |
| `{permission_mode}` | Permission mode |
| `{pr_number}` | PR number (review agents only) |

Default for work agents:
```
Work on issue #{issue_number}: {issue_title}. Follow the instructions in {prompt}. When done, use agent-done to report completion.
```

---

### Validation

Single validation command that runs on agent-done and pre-push:

```yaml
validation:
  cmd: null                 # (default) Validation command (e.g., "make validate")
  timeout_seconds: 300      # (default) 5 minutes
  # tracked = block on staged + unstaged changes (default)
  # unstaged = allow staged changes, block unstaged changes
  # off = disable dirty-check guard
  pre_push_dirty_check: "tracked"
  coverage_guardrail:
    enabled: false          # (default) Enforce per-file coverage on changed files
    min_percent: null       # Required when enabled (e.g., 85)
    apply_to: changed       # (default) "changed" or "all"
    scope: []               # File globs to check (e.g., ["src/issue_orchestrator/**"])
    coverage_type: line     # "line" or "branch"
    exclude: []             # File globs to exclude
```

When `cmd` is set:
- Runs after agent calls `agent-done` - gives immediate feedback
- Runs on `git push` - cached by SHA, instant pass if already validated

---

### Code Review

```yaml
review:
  enabled: false                              # (default)
  default: null                               # (default) Default reviewer agent label
  code_review_label: "needs-code-review"      # (default)
  code_reviewed_label: "code-reviewed"        # (default)
  max_rework_cycles: 2                        # (default) Before escalating to needs-human
```

#### Triage Review (Batch)

```yaml
review:
  triage_review_agent: null                   # (default) Agent for batch reviews
  triage_reviewed_label: "triage-reviewed"    # (default)
  triage_review_threshold: 0                  # (default) Auto-trigger after N PRs (0 = manual)
  triage_review_on_failure: true              # (default) Trigger triage on session failures
```

---

### Labels


```yaml
labels:
  in_progress: "in-progress"
  blocked: "blocked"
  needs_human: "needs-human"
  needs_rework: "needs-rework"
  validation_failed: "validation-failed"
  prefix: null
```

### review

```yaml
review:
  enabled: false
  default: null
  code_review_label: "needs-code-review"
  code_reviewed_label: "code-reviewed"
  triage_review_agent: null
  triage_review_label: null
  triage_reviewed_label: "triage-reviewed"
  triage_review_threshold: 0
  triage_review_on_failure: true
  max_rework_cycles: 10
  reviewer_feedback_cache_minutes: 5
```

- `reviewer_feedback_cache_minutes`: When a reviewer requests changes, the feedback is saved locally. Rework sessions started within this time window read feedback from the local file instead of fetching from GitHub API (which may have eventual consistency delays). Set to `-1` to disable local caching and always fetch from GitHub. Default: `5` minutes.

### cleanup

```yaml
cleanup:
  with_triage:
    close_ai_session_tabs: true
    remove_worktrees: false
  without_triage:
    wait_for_code_review: true
    close_ai_session_tabs: true
    remove_worktrees: false
```

### validation

```yaml
validation:
  cmd: null
  timeout_seconds: 300
  pre_push_dirty_check: "tracked"
```

- `validation.pre_push_dirty_check`: Controls pre-push dirty checks for tracked files. Values:
  - `"tracked"`: block on staged + unstaged changes (default)
  - `"unstaged"`: allow staged changes, block unstaged changes
  - `"off"`: disable the dirty-check guard

This guard prevents “validated one commit, pushed another” mistakes by requiring
the working tree (tracked files only) to be clean before validation and push.
Ignored/untracked files are not considered, so build caches and logs won’t block
you. Adjust the strictness with `validation.pre_push_dirty_check` if needed.

### retry

```yaml
retry:
  max_validation_retries: 3
  retry_prompt_template: null
```

- `retry.max_validation_retries`: Maximum number of times to retry after validation failure (default: 3)
- `retry.retry_prompt_template`: Path to default retry prompt template file (relative to repo root). Used when validation fails and agent needs to fix errors. If not set, uses built-in default template.

**Custom retry templates** support these variables:
- `{original_task}` - The original task/prompt
- `{validation_cmd}` - The command that failed
- `{error_file}` - Path to the full error output
- `{error_summary}` - Truncated error output (max 2000 chars)
- `{retry_count}` - Current attempt number (1-based)
- `{max_retries}` - Total allowed attempts

Example custom template at `.prompts/retry.md`:
```markdown
# Validation Failed (Attempt {retry_count}/{max_retries})

Your changes broke the build. Original task: {original_task}

Command: `{validation_cmd}`

Errors:
```
{error_summary}
```

Fix the errors and run `agent-done completed` when done.
If you cannot fix it, run `agent-done blocked --reason "explanation"`.
```

### ui

```yaml
ui:
  mode: "web"
  web_port: 8080
  control_api_port: 19080
  queue_refresh_seconds: 600
  instances: 1                        # Number of orchestrator instances (for multi-orchestrator)
```

When `instances` is greater than 1, the Control Center spawns multiple orchestrator processes. Each instance gets:
- A unique `INSTANCE_ID` environment variable (e.g., `orchestrator-1`, `orchestrator-2`)
- Auto-assigned ports (no conflicts)
- Isolated worktree directories (`worktree_base/orchestrator-1/`, etc.)

Use with `claims.enabled: true` to coordinate which instance works on each issue.

### observability

```yaml
observability:
  session_no_output_seconds: 120
  session_no_output_tail_lines: 50
  session_no_output_max_bytes: 10000
  session_no_output_repeat_seconds: 120
  session_output_retention_runs: 7
  stale_escalation_ticks: 0
  comment_headings:
    implementation: "## Implementation"
    problems: "## Problems Encountered"
    pr_link: "## Pull Request"
    blocked: "## Blocked"
    needs_human: "## Needs Human Input"
```

### security

```yaml
security:
  enforce_hooks: true
  pre_push_hook: null
  dangerous:
    allow_unsupported_agents: false
```

### hooks

```yaml
hooks:
  ai_gate:
    interval_days: 7                    # Run AI gate tests every N days (0 = disabled)
    dangerous_allow_failure: false      # If true, warn only; if false, block on failure
```

AI gate tests exercise hook/execpolicy enforcement to verify that dangerous commands like `git push --no-verify` are blocked. This runs on first setup and periodically to ensure hooks remain effective.

### filtering

```yaml
filtering:
  label: null
  milestone: null
  milestones: []
  issue: null
  exclude_labels: []
  fetch_limit: 100
  max_to_start: 0
```

`milestones` and `exclude_labels` accept a list or comma-separated string.

### scheduling

```yaml
scheduling:
  default_priority_tier: 1
```

- `scheduling.default_priority_tier`: Priority tier to use when issue titles do not include a `[P?-nnn]` prefix (0-9). Default is `1` (P1 / medium).

### milestones

```yaml
milestones:
  sort: "due_date"
  sort_config: {}
  foundation: "M0"
```

### triage

```yaml
triage:
  inherit_labels: []
  explicit_labels: []
  milestone_strategy:
    inherit_from_issues: true
    explicit: null
  priority: null
```

- `triage.inherit_labels`: Labels to inherit from source PRs and their linked issues when creating a triage issue. Only labels that exist on the source PRs or their linked issues (referenced via `#123` in PR title/body) will be applied. Useful for propagating test-data markers or category labels to triage issues.
- `triage.explicit_labels`: Labels to always add to triage issues, regardless of source.
- `triage.milestone_strategy.inherit_from_issues`: When `true`, inherits milestone from linked issues (uses "latest" by default, can be set to "earliest").
- `triage.milestone_strategy.explicit`: Explicit milestone name to use instead of inheriting.
- `triage.priority`: Priority tier to prefix triage issue titles (e.g., `P0`..`P9`). This controls scheduling order for triage issues via `[P?-nnn]` titles.

### e2e

```yaml
e2e:
  enabled: false
  auto_run_interval_minutes: 30
  pytest_args: ["tests/e2e", "-v"]
  allow_retry_once: true
  quarantine_file: "tests/e2e/quarantine.txt"
  survive_restart: true
  stop_on_first_failure: false
  flake_threshold: 20
  flake_window_runs: 10
  pr_labels: []
```

- `e2e.stop_on_first_failure`: If `true`, stops pytest on first test failure (adds `-x` flag). Default `false` runs all tests.
- `e2e.flake_threshold`: Flip rate percentage (0-100) above which a test is flagged as flaky. Default `20`.
- `e2e.flake_window_runs`: Number of recent E2E runs to check when calculating flip rate. Default `10`.

### sqlite_backup

```yaml
sqlite_backup:
  enabled: true
  cadence_hours: 24
  check_interval_minutes: 60
  retention_daily: 14
  retention_weekly: 8
  enforce_on_startup: true
```

- `sqlite_backup.enabled`: Toggle automatic SQLite backups.
- `sqlite_backup.cadence_hours`: Minimum hours between backups.
- `sqlite_backup.check_interval_minutes`: How often to check whether backups are due.
- `sqlite_backup.retention_daily`: Number of daily backups to keep (0 disables daily tier).
- `sqlite_backup.retention_weekly`: Number of weekly backups to keep (0 disables weekly tier).
- `sqlite_backup.retention_daily`: Number of daily backups to keep.
- `sqlite_backup.retention_weekly`: Number of weekly backups to keep.
- `sqlite_backup.enforce_on_startup`: If cadence elapsed, force a backup on startup.

**Care & feeding**
- Backups live under `.issue-orchestrator/backups/sqlite/<db_key>/daily/` and `weekly/`.
- Restore: stop the orchestrator, copy the newest backup `.db` over the target DB, then restart.
- Doctor shows backup status and will warn if backups are overdue.
- Long-running orchestrators check for due backups every `sqlite_backup.check_interval_minutes`.

### state

```yaml
state:
  file: ".issue-orchestrator/state.json"
```

### config

```yaml
config:
  strict: false
```

### claims

Multi-orchestrator coordination. When multiple orchestrator instances work on the same repository, the claims system ensures only one orchestrator works on each issue at a time.

```yaml
claims:
  enabled: false                      # Enable multi-orchestrator coordination
  claimant_id: null                   # Unique ID for this instance (required if enabled)
  lease_seconds: 900                  # Claim lease duration (15 min default)
  renew_before_expiry_seconds: 300    # Renew when this much time remains (5 min)
```

**For multi-orchestrator deployments:**

Each orchestrator needs a unique `claimant_id`. Use environment variable substitution:

```yaml
claims:
  enabled: true
  claimant_id: "${ORCHESTRATOR_ID}"   # Set ORCHESTRATOR_ID=prod-west-1 in environment
```

If `claimant_id` is not set, it defaults to `orchestrator-{pid}` which changes on restart.

**Labels added by claims system:**
- `io:claimed` - Issue is being worked on by an orchestrator
- `blocked:claim-lost` - Work was interrupted because another orchestrator took over
- `blocked:stale-claim` - Claim expired without being released (orchestrator crashed)

### agents

```yaml
agents:
  "agent:developer":
    prompt: ".issue-orchestrator/prompts/developer.md"
    model: "sonnet"
    timeout_minutes: 45
    skip_review: false
    reviewer: null
    retry_prompt_template: null
```

**Agent fields:**
- `prompt`: Path to prompt template file (required, relative to repo root)
- `model`: AI model to use (e.g., "sonnet", "haiku", "opus")
- `timeout_minutes`: Session timeout (default: from `execution.concurrency.session_timeout_minutes`)
- `skip_review`: Skip code review for this agent's PRs (default: false)
- `reviewer`: Override default reviewer agent for this agent's PRs
- `retry_prompt_template`: Path to custom retry prompt template for validation failures (relative to repo root). If not set, uses `retry.retry_prompt_template` or built-in default.

---

## Settings Dialog Reference

The web dashboard settings dialog is driven by a Pydantic schema in `src/issue_orchestrator/infra/settings_schema.py`. The schema is the single source of truth for:
- Settings HTML form fields (rendered via Jinja2)
- GET/POST `/api/settings` serialization and validation
- Setup wizard defaults and labels
- Doctor checks (path validation, agent references)
- This documentation reference

Goal Pilot uses the standard agent configuration: define its prompt under `agents` and reference the label via `goal_pilot.agent`.

<!-- BEGIN AUTO-GENERATED CONFIG REFERENCE — regenerate via: pytest tests/unit/test_settings_schema.py::TestDriftDetection::test_config_reference_not_stale -->
# Settings Reference

_Auto-generated from settings schema._

## Concurrency

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `execution.concurrency.max_concurrent_sessions` | integer | `3` | Maximum parallel agent sessions |
| `execution.concurrency.session_timeout_minutes` | integer | `45` | Kill sessions after this duration |
| `ui.queue_refresh_seconds` | integer | `600` | How often to refresh the issue queue from GitHub (0 = manual only) |
| `scheduling.default_priority_tier` | integer | `1` | Priority tier to use when issue titles have no [P?-nnn] prefix (0-9) |

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
| `review.max_rework_cycles` | integer | `10` | Max times to re-queue work agent before escalating |
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
| `hooks.ai_gate.interval_days` | integer | `7` | Run AI gate tests every N days (0 = disabled) |
| `hooks.ai_gate.dangerous_allow_failure` | boolean | `False` | If true, warn only on AI gate test failure; if false, block orchestrator start |

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
| `worktrees.default_branch` | string | `main` | Branch to base new worktrees and PRs on (must exist on origin) |
| `worktrees.worktree_branch_on_recreate` | string | `delete` | What to do when recreating a worktree with existing branch |
<!-- END AUTO-GENERATED CONFIG REFERENCE -->
