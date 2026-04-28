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
  # Allow enough time for the repo's authoritative local PR gate to finish.
  # For issue-orchestrator this includes unit/integration/web/vscode slices.
  timeout_seconds: 1800

execution:
  isolation:
    mode: "standard"   # or "hardened"
```

The configured command should be the same command your repository treats as its
authoritative pre-push / pre-publish gate.

When you install repo guardrails with `issue-orchestrator setup-guardrails`, the
generated `scripts/verify-pr.sh` captures the selected config filename. If you
switch the repo to a different `.issue-orchestrator/config/*.yaml`, rerun
`setup-guardrails` so pre-push validation and cache lookups continue to use the
same config.

The canonical **pre-publish** gate is the worktree's effective `pre-push` hook:

- project hook first (`make validate-pr`, `scripts/verify-pr.sh`, etc.)
- orchestrator hook second (dirty-tree + banned test-skipping patterns)

The orchestrator runs that hook chain before the authenticated push so
push-time policy failures are discovered before publish. The real push still
keeps hooks enabled; when the commit and configured command match, the later
hook pass reuses the cached validation record instead of rerunning the command.
CI still mirrors the repo's required PR coverage in a clean environment.

## Runtime Artifact Ignores

Dirty-tree guards ignore orchestrator-managed runtime files so agents are not
blocked by session state, local tool caches, or Claude Code scheduling locks.
Built-in ignores cover `.issue-orchestrator/` runtime state and
`.claude/scheduled_tasks.lock`.

Target repositories can add repo-local runtime artifacts in
`.issue-orchestrator/runtime-ignore`. See
[`docs/user/configuration.md`](../user/configuration.md#ignore-repo-local-runtime-artifacts)
for the supported format and operator guidance.

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
