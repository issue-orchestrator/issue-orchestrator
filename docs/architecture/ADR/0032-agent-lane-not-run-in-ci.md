# ADR 0032: The live-agent test lane is not run in CI

**Status:** Accepted
**Date:** 2026-07-16

## Context

The `validate-agent` required check is largely hollow on GitHub: roughly 3
passed, 26 skipped, in ~2 seconds. The skipped tests
(`tests/integration/test_codex_execution.py`, `test_live_agent_chain.py`,
`test_claude_execution.py`) gate on `shutil.which("claude"/"codex")` probes that
return False on the runner, because `.github/workflows/validate.yml` installs
neither CLI. The same tests all run locally when the CLIs are present (29 collect
and pass).

This surfaced while adding Dependabot handling (PR #6795): a dependency whose
behavior is exercised *only* by this lane — `pexpect`, which drives agent PTY
spawning in `execution/agent_runner.py` — could pass CI while actually being
broken.

## Decision

**We do not run the live-agent lane in CI. It is verified locally instead.**

`is_claude_authenticated()` is a *live provider probe*. Wiring the CLIs into CI
would require:

- provider credentials in Actions secrets,
- token spend on every PR,
- LLM/network flakiness gating every merge.

That is a permanent tax on all PRs to pre-empt a rare, loud, one-commit-revert
failure. We reject it.

### How the gap is contained

- **Human PRs** run the lane through the pre-push hook (`_validate-pr-impl`
  includes `_validate-agent-impl`).
- **Dependency PRs** whose only coverage is this lane (currently `pexpect`) are
  not merged on a green CI check; they are verified via `make deps-batch`, which
  runs `validate-pr-raw` locally. See the `dependency-upgrades` skill.

### Residual risk

An agent-lane-only dependency that is not recognized as such, and not pulled
into a local batch, could merge without the lane running. Mitigation: keep the
agent-lane-only list in the `dependency-upgrades` skill current.

## Consequences

- `validate-agent` remains a required check for the tests it *can* run in CI;
  its live-agent portion is a local-only gate by design, not an oversight.
- The only change that would close this without the per-PR tax is a
  record/replay agent lane runnable in CI without live credentials. Not planned;
  a future ADR would supersede this one if that changes.

## Alternatives considered

- **Install the CLIs + credentials in CI.** Rejected: per-PR credential exposure,
  token cost, and flakiness, as above.
- **Drop `validate-agent` as a required check.** Rejected: it still provides real
  coverage for the non-live tests, and requiring it keeps the merge queue honest
  for those.
