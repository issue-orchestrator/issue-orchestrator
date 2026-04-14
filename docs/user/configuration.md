# Configuration

Configuration lives in `.issue-orchestrator/config/default.yaml` (or a named config like `main.yaml`).

---

## TL;DR - Starter Config to Get Running

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

---

## Environment Variable Substitution

Any string value in config can reference environment variables using `${VAR}` syntax:

```yaml
claims:
  claimant_id: "${ORCHESTRATOR_ID}"    # Expands to value of ORCHESTRATOR_ID env var

repo:
  github:
    token_env: "${GITHUB_TOKEN_VAR}"   # Works in any string field
```

If the referenced environment variable is not set, config loading fails with a clear error message showing which variable is missing and where it was referenced.

---

## Common Additions

**Limit concurrency**
```yaml
execution:
  concurrency:
    max_concurrent_sessions: 2
```

**Only process specific issues**
```yaml
filtering:
  label: "bot-ready"
  milestones: ["M1", "M2"]
  exclude_labels: ["test-data"]
```

**Enable code review**
```yaml
review:
  enabled: true
  default: "agent:reviewer"

agents:
  "agent:reviewer":
    prompt: ".issue-orchestrator/prompts/reviewer.md"
    model: "sonnet"
```

**Declare repo-scoped GitHub auth**
```yaml
repo:
  name: "BruceBGordon/tixmeup"
  github:
    token_env: "TIXMEUP_GITHUB_TOKEN"
    keyring_service: "tixmeup-github"
    keyring_username: "bruce"
```

Use `token_env` when the repo should read a specific environment variable.
Use `keyring_service` and `keyring_username` when the repo should read a
specific OS keyring entry. You can declare one or both.

When a repo declares `repo.github.token_env` or `repo.github.keyring_*`,
those sources become authoritative:
- `doctor` validates the configured source instead of a random global token
- Control Center start checks validate access to `repo.name`, not just `/user`
- startup fails clearly if the repo-scoped source is missing, instead of
  silently falling back to another token that may not have repo access

---

## What To Read Next

- Full reference (auto-generated): `docs/user/configuration_reference.md`
- Complete example config: `examples/config.example.yaml`

---

## Advanced Options (Teaser)

You can configure much more than the minimal setup, including:
- Automated code review and triage workflows
- E2E test runner with flake tracking
- Multi-orchestrator coordination (claims)
- Provider resilience (retry + circuit breaker)
- Observability thresholds and escalation
- Hook enforcement and safety guardrails

The web Settings dialog (when `ui.mode: web`) is always available, and you can always edit the raw YAML config file directly.
If you want to revisit setup, you can rerun the setup wizard on an existing config at any time.

---

## Settings Dialog Reference

The web dashboard settings dialog is driven by `src/issue_orchestrator/infra/settings_schema.py`. The schema is the single source of truth for:
- Settings HTML form fields (rendered via Jinja2)
- GET/POST `/api/settings` serialization and validation
- Setup wizard defaults and labels
- Doctor checks (path validation, agent references)
- Documentation reference (auto-generated)

Goal Pilot uses the standard agent configuration: define its prompt under `agents` and reference the label via `goal_pilot.agent`.
