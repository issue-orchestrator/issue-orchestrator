# Configuration

Configuration lives in `.issue-orchestrator/config/default.yaml` (or a named config like `main.yaml`).

---

## TL;DR - Minimal Config to Get Started

```yaml
# Required: At least one agent
agents:
  "agent:dev":
    prompt: ".issue-orchestrator/prompts/dev.md"
    model: "sonnet"

# Optional but recommended: validation command
validation:
  cmd: "make test"
  timeout_seconds: 300
```

That's it. Label an issue with `agent:dev` and start the orchestrator.

### Quick additions

**Limit concurrency:**
```yaml
concurrency:
  max_concurrent_sessions: 2
```

**Only process specific issues:**
```yaml
filter_label: "bot-ready"        # Only issues with this label
filter_milestone: "M1"           # Only issues in this milestone
filter_issue: 123                # Only this specific issue
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

## Reference

### Top-Level Settings

#### Repository

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `repo` | string | auto-detect from git | GitHub repository in `owner/repo` format (usually not needed) |
| `worktree_base` | path | `.issue-orchestrator/worktrees` | Base directory for git worktrees |

#### Concurrency

```yaml
concurrency:
  max_concurrent_sessions: 3     # Max parallel agent sessions
  session_timeout_minutes: 45    # Kill sessions after this duration
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `max_concurrent_sessions` | int | 3 | Maximum parallel agent sessions |
| `session_timeout_minutes` | int | 45 | Session timeout in minutes |

#### Filtering

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `filter_label` | string | none | Only process issues with this label |
| `filter_milestone` | string | none | Only process issues in this milestone |
| `filter_milestones` | list | [] | Process issues in any of these milestones |
| `filter_issue` | int | none | Only process this specific issue number |
| `issue_fetch_limit` | int | 100 | Max issues to fetch per API call |
| `max_issues_to_start` | int | 0 | Max issues to start (0 = unlimited) |

---

### Agents

Each agent key must match a GitHub label (e.g., `agent:dev` label triggers the `agent:dev` config).

```yaml
agents:
  "agent:dev":
    prompt: ".issue-orchestrator/prompts/dev.md"
    model: "sonnet"
    timeout_minutes: 45
    permission_mode: "default"
    skip_review: false
    reviewer: "agent:reviewer"    # Override default reviewer
    initial_prompt: "..."         # Custom first message
    command: "..."                # Custom shell command (advanced)
    ai_system: "claude"           # AI system: claude, codex, gemini
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `prompt` | path | required | Path to prompt markdown file |
| `model` | string | "sonnet" | Model: `haiku`, `sonnet`, or `opus` |
| `timeout_minutes` | int | 45 | Session timeout for this agent |
| `permission_mode` | string | "default" | Claude permission mode |
| `skip_review` | bool | false | Skip code review for this agent's PRs |
| `reviewer` | string | none | Override default reviewer for this agent |
| `initial_prompt` | string | see below | First message sent to Claude |
| `command` | string | auto | Custom shell command to run agent |
| `ai_system` | string | "claude" | AI system to use |

#### initial_prompt

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
  cmd: "make test"           # Command to run
  timeout_seconds: 300       # Timeout (default: 300 = 5 min)
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `cmd` | string | none | Validation command (e.g., `make test`) |
| `timeout_seconds` | int | 300 | Command timeout in seconds |

When configured:
- Runs after agent calls `agent-done` - gives immediate feedback
- Runs on `git push` - cached by SHA, instant pass if already validated

---

### Code Review

```yaml
review:
  enabled: true                          # Enable code review workflow
  default: "agent:reviewer"              # Default reviewer agent
  code_review_label: "needs-code-review" # Label for PRs awaiting review
  code_reviewed_label: "code-reviewed"   # Label after review passes
  max_rework_cycles: 2                   # Max rework attempts before escalation
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | false | Enable code review workflow |
| `default` | string | none | Default reviewer agent label |
| `code_review_label` | string | "needs-code-review" | Label for PRs needing review |
| `code_reviewed_label` | string | "code-reviewed" | Label after review passes |
| `max_rework_cycles` | int | 2 | Max rework cycles before `needs-human` |

#### Triage Review (Batch)

```yaml
review:
  triage_review_agent: "agent:triage"       # Agent for batch reviews
  triage_reviewed_label: "triage-reviewed"  # Label after triage
  triage_review_threshold: 5                # Trigger after N PRs (0 = manual)
  triage_review_on_failure: true            # Trigger on session failures
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `triage_review_agent` | string | none | Triage reviewer agent |
| `triage_reviewed_label` | string | "triage-reviewed" | Label after triage |
| `triage_review_threshold` | int | 0 | Auto-trigger after N PRs |
| `triage_review_on_failure` | bool | true | Trigger triage on failures |

---

### Labels

```yaml
labels:
  prefix: "bot"              # Optional prefix for all labels
  in_progress: "in-progress"
  blocked: "blocked"
  needs_human: "needs-human"
  needs_rework: "needs-rework"
  validation_failed: "validation-failed"
```

With `prefix: "bot"`, labels become `bot:in-progress`, `bot:blocked`, etc.

---

### UI and Web Dashboard

```yaml
ui_mode: "web"               # "web" (browser) or "tmux" (terminal)
web_port: 8080               # Web dashboard port
control_api_port: 19080      # Control API port (0 = disabled)
queue_refresh_seconds: 600   # How often to refresh from GitHub
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `ui_mode` | string | "web" | UI mode: `web` or `tmux` |
| `web_port` | int | 8080 | Web dashboard port |
| `control_api_port` | int | 19080 | Control API port |
| `queue_refresh_seconds` | int | 600 | GitHub refresh interval |

---

### GitHub API

```yaml
github_token_env: "GITHUB_TOKEN"        # Env var for token
github_api_url: "https://api.github.com"
github_http_timeout_seconds: 20.0
github_cache_ttl_seconds: 300
github_required_scopes: []              # Required OAuth scopes
github_allowed_scopes: []               # Allowed OAuth scopes
```

#### Rate Limit Monitoring

```yaml
gh_rate_limit_startup: true          # Log rate limits at startup
gh_rate_limit_every_calls: 500       # Check every N calls (0 = disabled)
gh_rate_limit_warn_fraction: 0.1     # Warn below this fraction remaining
gh_rate_limit_warn_remaining: 100    # Warn below this count
```

#### Write Verification

```yaml
gh_write_verify_timeout_seconds: 20
gh_write_verify_initial_delay_ms: 250
gh_write_verify_max_delay_ms: 2000
gh_write_verify_backoff: 1.5
gh_write_verify_jitter_ms: 0
```

#### Audit

```yaml
gh_audit_enabled: false              # Enable audit reporting
gh_audit_events: false               # Emit audit to event stream
gh_audit_file: "gh-audit-{pid}.json" # Audit file path
```

---

### Session Detection

```yaml
session_no_output_seconds: 120       # Emit warning after N seconds idle
session_no_output_tail_lines: 50     # Lines to include in warning
session_no_output_max_bytes: 10000   # Max bytes of tail content
session_no_output_repeat_seconds: 120 # Min gap between warnings
session_grace_period_seconds: 120    # Don't terminate young sessions
session_log_activity_seconds: 120    # Log activity window
```

---

### Cleanup

```yaml
cleanup:
  with_triage:                       # When triage review is enabled
    close_ai_session_tabs: true
    remove_worktrees: false
  without_triage:                    # When triage is NOT enabled
    wait_for_code_review: true       # Wait for review before cleanup
    close_ai_session_tabs: true
    remove_worktrees: false

# Legacy (deprecated - use cleanup section)
close_completed_tabs: true
close_failed_tabs: false
```

---

### Hooks and Worktree Setup

```yaml
enforce_hooks: true                  # Install pre-push hooks
pre_push_hook: null                  # Custom hook path (uses bundled if null)
setup_worktree:                      # Commands after worktree creation
  - "npm install"
  - "pip install -e ."
```

---

### Terminal and Tmux

```yaml
terminal_adapter: null               # Override: "builtin:tmux" or custom
tmuxp: ".issue-orchestrator/tmuxp.yaml"  # Custom tmuxp config
tmux_bindings:                       # Tmux key bindings
  - "bind-key -T root DoubleClick1Pane resize-pane -Z -t ="
```

---

### Milestone Sorting

```yaml
milestone_sort: "due_date"           # Strategy: due_date, number, pattern, name
milestone_sort_config: {}            # Strategy-specific config
foundation_milestone: "M0"           # Dependencies must be in same or foundation
```

---

### Isolation

```yaml
isolation:
  mode: "standard"                   # "standard" or "hardened"
```

---

### Dangerous Options

```yaml
dangerous:
  allow_unsupported_agents: false    # Allow agents without hook support
```

---

### Stale Detection

```yaml
stale_escalation_ticks: 0            # Escalate after N ticks stale (0 = disabled)
```

---

### Comment Headings

Customize headings in agent comments:

```yaml
comment_headings:
  implementation: "## Implementation"
  problems: "## Problems Encountered"
  pr_link: "## Pull Request"
  blocked: "## Blocked"
  needs_human: "## Needs Human Input"
```

---

### E2E Testing

```yaml
e2e_pr_labels:                       # Labels for E2E test PRs
  - "e2e-test"
```

---

### Config Validation

```yaml
config:
  strict: false                      # If true, unknown fields cause errors
```
