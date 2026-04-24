# Validation System

Validation is a **publish gate**, not a CI system.

## Model

- Run one user-defined local command per suite
- Cache results by worktree + commit SHA
- Reuse across feedback/publish hooks
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

`make validate` is the fast local validation command used during agent feedback.

The canonical **pre-publish** gate is the worktree's effective `pre-push` hook:

- project hook first (`make validate-pr`, `scripts/verify-pr.sh`, etc.)
- orchestrator hook second (dirty-tree + banned test-skipping patterns)

The orchestrator now runs that hook chain before the authenticated push so push-time policy failures are discovered before publish. CI still mirrors the repo's required PR coverage in a clean environment.

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
