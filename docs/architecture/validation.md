# Validation System

Validation is a **publish gate**, not a CI system.

## Model

- Run one user-defined local command per suite
- Cache passing results by worktree + commit SHA + command
- Reuse the same passing record across pre-publish and pre-push hooks
- Observe GitHub CI rather than reproducing it locally

## Configuration (YAML)

```yaml
validation:
  cmd: "make validate-pr"
  timeout_seconds: 1800

execution:
  isolation:
    mode: "standard"   # or "hardened"
```

The configured command should be the same command your repository treats as its
authoritative pre-push / pre-publish gate.

The canonical **pre-publish** gate is the worktree's effective `pre-push` hook:

- project hook first (`make validate-pr`, `scripts/verify-pr.sh`, etc.)
- orchestrator hook second (dirty-tree + banned test-skipping patterns)

The orchestrator runs that hook chain before the authenticated push so
push-time policy failures are discovered before publish. The real push still
keeps hooks enabled; when the commit and configured command match, the later
hook pass reuses the cached validation record instead of rerunning the command.
CI still mirrors the repo's required PR coverage in a clean environment.

## Record Format

Location: `.issue-orchestrator/validation/<HEAD_SHA>.json`

Record fields:
- `schema_version`
- `suite`
- `head_sha`
- `passed` + `exit_code`
- `command`
- `started_at` / `ended_at`
- `stdout`/`stderr` paths (optional but recommended)
