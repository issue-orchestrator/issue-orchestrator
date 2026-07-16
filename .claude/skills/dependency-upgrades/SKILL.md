---
name: dependency-upgrades
description: Upgrade dependencies and handle Dependabot PRs. Use when clearing a Dependabot backlog, running the weekly dependency batch, deciding what to do when a dependency upgrade breaks tests, deciding whether a Dependabot PR is safe to merge directly, adding an ignore/pin, or changing .github/dependabot.yml.
---

# Dependency Upgrades

How this repo upgrades dependencies and handles Dependabot PRs. Two mechanisms,
one policy.

## Policy: fix forward, always. No pins.

We are strongly biased to make upgrades work. When an upgrade breaks something,
**adapt our code to the new version** — do not pin, revert, or `ignore` it.

Being on HEAD is what keeps breaks cheap: you hit each change one release after
it lands, in isolation, with a clear signal about what broke. Pinning trades
that away — six months later you face the pinned version *plus* everything after
it, all at once, which is the big-bang migration this process exists to prevent.
A pin is deferred interest, not avoided work, and it silently blocks that
package's security patches while it sits.

The weekly cadence is the mechanism that makes fix-forward safe: breaks stay
small enough to fix inside the window. So there is no "defer the upgrade" option.
The only question is **which PR the fix lands in**:

- **In the batch** — default. Nearly always.
- **In its own PR** — only when the fix is a subsystem redesign that would
  otherwise ride along with a pile of lockfile bumps and either get
  rubber-stamped or stall the batch. This is a review-surface call, not
  tail-risk hedging. Both are still fix-forward.

If a fix genuinely cannot land this week, **skip that week's batch** — fix it,
resume next week. Do not pin to buy time. You lose one week of the *batched*
ecosystems; security and CI-covered updates keep flowing through auto-merge the
whole time (see below).

### The one real exception

**Security updates never defer.** If a security upgrade needs a large fix, it
still gets fixed now — escalate, don't queue. Staying on a known-vulnerable
version is not an acceptable resting state.

### If you ever think you need a pin

You almost certainly don't (the cadence handles it). The legitimate uses are:
- a hard, external incompatibility with another dependency (not our code), or
- temporary scaffolding **with a live fix PR already open**.

Either way the constraint must be self-documenting and greppable, so "which
constraints are stale?" is a `grep`, not archaeology:

```toml
"fastapi>=0.104,<0.139",  # TEMP: PR #NNNN in flight — 0.139 route internals; remove when it lands
```

A pin with no open PR and no external-conflict reason is a policy violation, not
a judgment call.

## How Dependabot PRs get merged

There is **no auto-merge**. Dependabot opens grouped PRs; a human merges them.
Two paths, and **coverage decides which — not semver**. semver-major is
upstream's claim about *their* API; our exposure depends on whether our suite
exercises the code. (In PR #6795, a starlette *minor* broke route registration
while rich and textual *majors* passed clean.)

### Merge the PR directly — when CI genuinely covers it

For `uv` and `github-actions` PRs, `validate-fast` (typecheck, lint, ~9,100
unit/integration/web tests) is a comprehensive gate. When it is green you can
merge the Dependabot PR as-is; the merge queue re-runs the checks against the
real merge result before it lands.

**Do NOT trust the green check alone for:**
- **npm** — CI compiles the extension (`validate-vscode`) but cannot run it
  (`test-vscode` self-skips on Actions). Verify via `make deps-batch` first.
- **Agent-lane-only dependencies** (list below) — their only real coverage is
  the live-agent lane, which CI cannot run (no claude/codex CLI on the runner).
  A green check is misleading. Verify via `make deps-batch` first.

#### Agent-lane-only dependencies

Their behavior is exercised *solely* by `test-simulated-agent` /
`test-integration-agent`, which skip on GitHub:

- `pexpect` — drives agent PTY spawning (`execution/agent_runner.py`); its PTY
  tests (`tests/integration/test_live_agent_chain.py`) skip on GitHub.

A grouped PR that includes one of these needs local verification as a whole —
`make deps-batch` upgrades the group together and runs the lane, so that pass
covers the group.

### Verify locally with `make deps-batch` — for the rest

Local `make deps-batch` runs `validate-pr-raw`, a strict superset of CI: it runs
the VS Code harness and the live-agent lane, neither of which CI can. This is the
only place npm and the agent-lane deps get real coverage.

```bash
git worktree add ../issue-orchestrator-wt-deps -b deps-YYYY-MM
cd ../issue-orchestrator-wt-deps && make worktree-setup

make deps-batch           # Python (root + tools/semgrep) fully; npm within ^ ranges
make deps-batch MAJOR=1   # also bump npm package.json ranges (see caution)

# deps-batch runs the FULL required suite (validate-pr-raw): static + core tests
# + live-web + the agent lane + test-vscode. Read the manifest diff it prints.
git add -A && git commit --signoff && git push -u origin deps-YYYY-MM
gh pr create
```

You never close Dependabot PRs by hand — Dependabot closes its own once the
versions it proposed land on main.

## Gotchas (cost real time in PR #6795)

- **`deps-batch` must call `validate-pr-raw`, not `validate`.** `make validate`
  stops at `_validate-impl` and omits `_validate-agent-impl`, so it skips the
  live-agent lane — the exact lane the local batch exists to cover.
- **Route introspection lives in `tests/unit/route_helpers.py` only.** FastAPI
  0.139+ no longer flattens `include_router` into `app.routes` (lazy
  `_IncludedRouter` wrappers, no `.path`). No public flatten API exists.
- **VS Code compile floor and runtime version are pinned separately.**
  `@types/vscode` is pinned (`~`) to the `engines.vscode` floor so `tsc` compiles
  against the minimum declared API (guarded by
  `test_vscode_types_pinned_to_engines_floor`); raise both together. The test
  *runtime* is pinned via `DEFAULT_VSCODE_VERSION` in
  `packages/vscode/test/runTest.ts` and may be newer (the harness cannot launch
  old builds). Unpinned, the runtime chased `stable` and timed out on a 273MB
  download.
- **`MAJOR=1` uses npm-check-updates, which overshoots Dependabot** (it targeted
  typescript 7.0.2 vs Dependabot's 6.0.3, and bumped a runtime dep Dependabot
  never proposed). Read the `package.json` diff before committing.
- **`packages/agent_runner` is deliberately not Dependabot-managed** —
  `dependencies = []`, consumed via `pythonpath`, never installed.

## Known, accepted gap

`validate-agent` is a required check but is largely hollow on GitHub (~3 passed,
26 skipped, ~2s) because the runner has no claude/codex CLI. This is why
agent-lane-only dependencies need local verification before merging. Installing
the CLIs in CI is deliberately *not* done: `is_claude_authenticated()` is a live
provider probe, so it would put credentials in secrets and add token spend + LLM
flakiness to every PR. `make deps-batch` covers the lane locally instead.
Recorded as an accepted trade-off in ADR-0032.
