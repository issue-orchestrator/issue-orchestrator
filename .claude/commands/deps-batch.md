---
description: Drive the weekly dependency batch end-to-end — upgrade to HEAD, fix-forward breaks, open a ready-to-merge PR.
argument-hint: "[--no-major] [--branch NAME]"
---

Drive this repo's weekly dependency batch to completion. Follow the
`dependency-upgrades` skill for policy (**fix forward, no pins, security never
defers**) — load it if it isn't already in context.

You are running as the **human maintainer's hands** (interactive Claude), so you
MAY push and open a PR. You must **never merge** — a human merges. If you find
yourself in an orchestrated coding-agent sandbox (push/PR blocked by hooks),
stop and complete via `coding-done` instead.

Arguments (optional): `$ARGUMENTS` — pass straight through to the spine script
(e.g. `--no-major` to hold npm within existing ranges, `--branch NAME`).

## Steps

1. **Run the spine, review-mode (no `--pr`):**
   `scripts/deps-batch-drive.sh $ARGUMENTS`
   It creates a fresh `deps-YYYY-MM` worktree off `origin/main`, runs
   `make worktree-setup`, then `make deps-batch MAJOR=1` (upgrade + full local
   `validate-pr-raw`). Note the worktree path it prints; you will work there.

2. **If the script exits non-zero (verification failed) → fix forward.**
   `cd` into the worktree it created (`../issue-orchestrator-wt-deps-YYYY-MM`).
   Diagnose the break and **adapt our code to the new versions** — do not pin,
   revert, or add an `ignore`. Likely suspects are npm majors (typescript,
   `@types/node`, mocha, eventsource) and any route/ASGI surface (starlette).
   Route introspection lives only in `tests/unit/route_helpers.py`. Re-run
   `make deps-batch MAJOR=1` until green. If a fix genuinely cannot land this
   week, skip the batch (fix next week) — **except security updates, which never
   defer: escalate and fix now.**

3. **Review the diff.** `git diff --stat` on `uv.lock`, `tools/semgrep/uv.lock`,
   `packages/vscode/package.json`, `packages/vscode/package-lock.json`. `MAJOR=1`
   uses npm-check-updates, which can overshoot Dependabot (it has bumped runtime
   deps Dependabot never proposed) — sanity-check `package.json` before
   committing. Surface anything surprising to the user.

4. **Commit, push, open the PR (do NOT merge).** From the worktree:
   ```
   git add -A
   git commit --signoff -m "Batch dependency upgrade <branch> (manual merge)"
   git push -u origin <branch>        # pre-push re-runs validate-pr-raw — expected
   gh pr create --base main --title "Batch dependency upgrade <branch> (manual merge)" --body "..."
   ```
   (You can instead re-run the spine with `--pr`, but doing the commit yourself
   lets you write an accurate body describing any fix-forward changes.)

5. **Flag direct-merge-safe Dependabot PRs.** Some ecosystems aren't part of the
   batch — notably **github-actions** (workflow YAML pins). List any open
   Dependabot PRs whose checks are green and which the skill says are safe to
   merge on a green check (github-actions, plain uv groups). Do **not** merge
   them; present them for the user's one-click merge. Pre-batch Python PRs that
   the upgrade superseded are left alone — Dependabot auto-closes its own once
   the versions land on main.

## Report back
- The PR URL (unmerged) and a one-line summary of what upgraded.
- Every fix-forward change you made, and why (per the skill's coding output
  requirement).
- The list of green, direct-merge-safe Dependabot PRs for the user to merge.
- Anything you skipped or that needs a human decision.
