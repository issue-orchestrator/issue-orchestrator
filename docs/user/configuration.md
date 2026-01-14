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
```

### execution

```yaml
execution:
  concurrency:
    max_concurrent_sessions: 3
    session_timeout_minutes: 45
  terminal_adapter: null
  tmux_session_mode: "shared"
  tmuxp: null
  tmux_bindings:
    - "bind-key -T root DoubleClick1Pane resize-pane -Z -t ="
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
```

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

### ui

```yaml
ui:
  mode: "web"
  web_port: 8080
  control_api_port: 19080
  queue_refresh_seconds: 600
```

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
