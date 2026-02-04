# Validation System

Validation is a **publish gate**, not a CI system.

## Model

- Run one user-defined local command per suite
- Cache results by worktree + commit SHA
- Reuse across feedback/publish/hooks
- Observe GitHub CI rather than reproducing it locally
- Validation scripts are owned by the **target repo**. The orchestrator only invokes
  `validation.script` and passes context on stdin.

## Configuration (YAML)

```yaml
validation:
  script: "repo-guardrails/validate/run.sh"
  args: []
  env: {}
  cmd: null  # legacy
  timeout_seconds: 1800
  pre_push_dirty_check: "tracked"

execution:
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
