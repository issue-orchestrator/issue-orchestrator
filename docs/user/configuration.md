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
  setup: []
  reuse_push_preflight: true
  allow_no_verify_dry_run_preflight: true
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

### labels

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
  max_rework_cycles: 2
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
```

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
- `triage.priority`: Priority label to add to triage issues (e.g., "priority:high").

### e2e

```yaml
e2e:
  enabled: false
  auto_run_interval_minutes: 30
  pytest_args: ["tests/e2e", "-v"]
  allow_retry_once: true
  quarantine_file: "tests/e2e/quarantine.txt"
  survive_restart: true
  pr_labels: []
```

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
