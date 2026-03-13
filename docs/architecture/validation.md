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
  cmd: "make validate"
  timeout_seconds: 1800

execution:
  isolation:
    mode: "standard"   # or "hardened"
```

`make validate` is the fast local publish gate. CI layers additional required coverage on top of it by running the agent-backed simulated and integration slices separately.

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
