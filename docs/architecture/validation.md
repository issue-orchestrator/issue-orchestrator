# Validation System

Validation is a **publish gate**, not a CI system.

## Model

- Run one user-defined local command per suite
- Cache results by worktree + commit SHA
- Reuse across feedback/publish/hooks
- Observe GitHub CI rather than reproducing it locally

## Configuration (YAML)

```yaml
validation:
  publish_gate:
    cmd: "make validate"
    timeout_seconds: 1800
  agent_gate:
    cmd: "make validate-fast"
    timeout_seconds: 600

validation_policy:
  agent_runs: "agent_gate"
  publish_requires: "publish_gate"

isolation:
  mode: "standard"   # or "hardened"
```

## Record Format

Location: `.issue-orchestrator/validation/<suite>/<HEAD_SHA>.json`

Record fields:
- `schema_version`
- `suite`
- `head_sha`
- `passed` + `exit_code`
- `command`
- `started_at` / `ended_at`
- `stdout`/`stderr` paths (optional but recommended)
