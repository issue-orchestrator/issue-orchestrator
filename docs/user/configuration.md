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
filter_label: "bot-ready"             # Single label only
filter_milestone: "M1"                # Single milestone only
filter_milestones: ["M1", "M2"]       # Multiple milestones (list)
filter_issue: 123                     # Single issue number only
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

All settings below show their **default values**. Omit any setting to use its default.

---

### Top-Level Settings

#### Repository

```yaml
repo: null                                        # (default: auto-detect from git remote)
worktree_base: ".issue-orchestrator/worktrees"    # (default)
```

#### Concurrency

```yaml
concurrency:
  max_concurrent_sessions: 3    # (default)
  session_timeout_minutes: 45   # (default)
```

#### Filtering

```yaml
filter_label: null              # (default) Only process issues with this label
filter_milestone: null          # (default) Only process issues in this milestone (single)
filter_milestones: []           # (default) Process issues in any of these milestones (list)
filter_issue: null              # (default) Only process this specific issue number
issue_fetch_limit: 100          # (default) Max issues to fetch per API call
max_issues_to_start: 0          # (default) Max issues to start (0 = unlimited)
```

`filter_milestones` accepts a list or comma-separated string:
```yaml
filter_milestones: ["M1", "M2"]     # list format
filter_milestones: "M1, M2"         # comma-separated string (equivalent)
```

---

### Agents

Each agent key must match a GitHub label (e.g., `agent:dev` label triggers the `agent:dev` config).

```yaml
agents:
  "agent:dev":
    prompt: ".issue-orchestrator/prompts/dev.md"  # (required)
    model: "sonnet"              # (default) Options: haiku, sonnet, opus
    timeout_minutes: 45          # (default)
    permission_mode: "default"   # (default)
    skip_review: false           # (default) Skip code review for this agent's PRs
    reviewer: null               # (default) Override default reviewer for this agent
    initial_prompt: null         # (default: see below) Custom first message
    command: null                # (default: auto) Custom shell command (advanced)
    ai_system: "claude"          # (default) Options: claude, codex, gemini
```

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
  cmd: null                 # (default) Validation command (e.g., "make test")
  timeout_seconds: 300      # (default) 5 minutes
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
  prefix: null                    # (default) Optional prefix for all labels
  in_progress: "in-progress"      # (default)
  blocked: "blocked"              # (default)
  needs_human: "needs-human"      # (default)
  needs_rework: "needs-rework"    # (default)
  validation_failed: "validation-failed"  # (default)
```

With `prefix: "bot"`, labels become `bot:in-progress`, `bot:blocked`, etc.

---

### UI and Web Dashboard

```yaml
ui_mode: "web"                    # (default) Options: web, tmux
web_port: 8080                    # (default)
control_api_port: 19080           # (default) 0 = disabled
queue_refresh_seconds: 600        # (default) How often to refresh from GitHub
```

---

### GitHub API

```yaml
github_token_env: null                        # (default: uses GITHUB_TOKEN or gh auth)
github_api_url: "https://api.github.com"      # (default)
github_http_timeout_seconds: 20.0             # (default)
github_cache_ttl_seconds: 300                 # (default)
github_required_scopes: []                    # (default) Required OAuth scopes (list)
github_allowed_scopes: []                     # (default) Allowed OAuth scopes (list)
```

Scope settings accept a list or comma-separated string:
```yaml
github_required_scopes: ["repo", "read:org"]  # list format
github_required_scopes: "repo, read:org"      # comma-separated string (equivalent)
```

#### Rate Limit Monitoring

```yaml
gh_rate_limit_startup: true       # (default) Log rate limits at startup
gh_rate_limit_every_calls: 500    # (default) Check every N calls (0 = disabled)
gh_rate_limit_warn_fraction: 0.1  # (default) Warn below this fraction remaining
gh_rate_limit_warn_remaining: 100 # (default) Warn below this count
```

#### Write Verification

```yaml
gh_write_verify_timeout_seconds: 20   # (default)
gh_write_verify_initial_delay_ms: 250 # (default)
gh_write_verify_max_delay_ms: 2000    # (default)
gh_write_verify_backoff: 1.5          # (default)
gh_write_verify_jitter_ms: 0          # (default)
```

#### Audit

```yaml
gh_audit_enabled: false           # (default)
gh_audit_events: false            # (default) Emit audit to event stream
gh_audit_file: null               # (default) Path for audit file (supports {pid})
```

---

### Session Detection

```yaml
session_no_output_seconds: 120        # (default) Emit warning after N seconds idle
session_no_output_tail_lines: 50      # (default) Lines to include in warning
session_no_output_max_bytes: 10000    # (default) Max bytes of tail content
session_no_output_repeat_seconds: 120 # (default) Min gap between warnings
session_grace_period_seconds: 120     # (default) Don't terminate young sessions
session_log_activity_seconds: 120     # (default) Log activity window
```

---

### Cleanup

```yaml
cleanup:
  with_triage:                        # When triage review is enabled
    close_ai_session_tabs: true       # (default)
    remove_worktrees: false           # (default)
  without_triage:                     # When triage is NOT enabled
    wait_for_code_review: true        # (default) Wait for review before cleanup
    close_ai_session_tabs: true       # (default)
    remove_worktrees: false           # (default)

# Legacy (deprecated - use cleanup section)
close_completed_tabs: true            # (default)
close_failed_tabs: false              # (default)
```

---

### Hooks and Worktree Setup

```yaml
enforce_hooks: true               # (default) Install pre-push hooks
pre_push_hook: null               # (default: uses bundled hook)
setup_worktree: []                # (default) Commands after worktree creation
```

Example setup commands:
```yaml
setup_worktree:
  - "npm install"
  - "pip install -e ."
```

---

### Terminal and Tmux

```yaml
terminal_adapter: null            # (default: auto) Override: "builtin:tmux" or custom
tmuxp: null                       # (default) Custom tmuxp config file path
tmux_bindings:                    # (default: double-click to zoom)
  - "bind-key -T root DoubleClick1Pane resize-pane -Z -t ="
```

---

### Milestone Sorting

```yaml
milestone_sort: "due_date"        # (default) Options: due_date, number, pattern, name
milestone_sort_config: {}         # (default) Strategy-specific config
foundation_milestone: "M0"        # (default) Dependencies must be in same or foundation
```

---

### Isolation

```yaml
isolation:
  mode: "standard"                # (default) Options: standard, hardened
```

---

### Dangerous Options

```yaml
dangerous:
  allow_unsupported_agents: false # (default) Allow agents without hook support
```

---

### Stale Detection

```yaml
stale_escalation_ticks: 0         # (default) Escalate after N ticks stale (0 = disabled)
```

---

### Comment Headings

Customize headings in agent comments:

```yaml
comment_headings:
  implementation: "## Implementation"       # (default)
  problems: "## Problems Encountered"       # (default)
  pr_link: "## Pull Request"                # (default)
  blocked: "## Blocked"                     # (default)
  needs_human: "## Needs Human Input"       # (default)
```

---

### E2E Testing

```yaml
e2e_pr_labels: []                 # (default) Labels for E2E test PRs
```

---

### Config Validation

```yaml
config:
  strict: false                   # (default) If true, unknown fields cause errors
```
