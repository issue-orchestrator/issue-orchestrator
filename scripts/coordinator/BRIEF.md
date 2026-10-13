> **UPDATE 2026-10-11: integration efforts are explicit.** Work rolls into ONE integration branch at a time — currently `tech-lead-improver` (reset to main 4ce2bac after delivery #9477). An issue belongs to it iff it carries the label `integrate:tech-lead-improver`. Only work labelled issues; base your branch on `origin/tech-lead-improver` and open your PR against it. The coordinator merges approved PRs into it and delivers the branch to main as one PR on a cadence (about weekly or ~100 commits), then resets it to main. Milestones stay roadmap themes; never use them for routing. A second concurrent effort needs its own branch + label + engine; do not start one without the operator.

> **UPDATE 2026-10-07: two locks, not one.** Exam runs (`make test-tech-lead-exam`, improver exams) now take `~/dev/worktree/issue-orchestrator/.coord/exam.lock`. `make validate-pr` gates and pushes keep `.coord/gate.lock`. An exam and a gate may run at the same time; two exams may not, and neither may two gates. A run already in flight keeps the lock it holds; switch from your next run.

> **UPDATE 2026-10-04 — coordination files MOVED (the /tmp scratchpad gets wiped).**
> - This brief now lives at `~/dev/worktree/issue-orchestrator/.coord/BRIEF.md`.
> - The shared gate lock is now `~/dev/worktree/issue-orchestrator/.coord/gate.lock` (take it with `mkdir`, release with `rmdir`; never rmdir one you didn't take). For any NEW gate, exam or push run, take the new lock AND wait until the old scratchpad lock (`.../scratchpad/agents/gate.lock`) is absent. A run already in flight keeps the lock it holds.
> - Put durable logs under `.coord/exam/`, not the scratchpad.

> **UPDATE 2026-09-28 (2):** all work now integrates on feature branch **`tech-lead-improver`** (cut from main after #7488). Base new branches on `origin/tech-lead-improver` and open PRs against it; the COORDINATOR merges approved PRs into it (merge commits) and delivers it to main as one commit. The older FEATURE BRANCH section below applies with `tech-lead-reliability` → `tech-lead-improver`. Porchpin's engine RUNS this branch, so a merge here reaches a live engine: be conservative. Never open porchpin's live SQLite DBs, even `mode=ro` (it writes to -shm); byte-copy db, -wal and -shm first.

> **Porchpin is READ-ONLY, including its git repo:** never run `git fetch`, `git checkout`, `git worktree` or any other writing git command in `~/dev/porchpin`. Fetching creates refs and adds objects. For git history, make a scratch clone: `git clone --shared ~/dev/porchpin <scratch>` and fetch there.

# Shared brief for follow-up issue agents (issue-orchestrator repo)

You are one of several agents each fixing ONE GitHub issue in issue-orchestrator/issue-orchestrator, in parallel. Read ~/dev/issue-orchestrator/CLAUDE.md first and follow it exactly (hexagonal owners, fail-fast, no fallbacks, tests at port boundaries, Final Abstraction Pass, UI accessibility rules if you touch UI).

## Setup
- Base clone: ~/dev/issue-orchestrator. NEVER edit files there.
- Create your worktree: `cd ~/dev/issue-orchestrator && git fetch -q origin && make worktree-create BRANCH=<your-branch> BASE_REF=origin/main WORKTREE_PATH=$HOME/dev/worktree/issue-orchestrator/<your-branch>` and work only there. Activate its `.venv`; run tests as `PYTHONPATH="$(pwd)/src" python -m pytest ...`.
- Read your issue with `gh issue view <n> --comments`. The raw REST subcommand of the gh CLI is BLOCKED by a repo hook (even inside heredocs/strings); use `gh issue` / `gh pr` subcommands only. Calling gh from Python in src is forbidden.

## Engineering bar
- Fix the class, not the instance: enumerate every call site/path of the behaviour first.
- Every new test must go RED when the production change is reverted — actually revert and run it (then restore). A test that stays green proves nothing.
- Don't add `# noqa`; don't raise any quality-ratchet budget (`quality/guardrails-baseline.json`). If a control file is at its line budget, move logic to its owner module instead. Duplicate identical noqa lines are LINE-keyed: if you insert code above existing ones, use `python tools/quality_guardrails.py --accept <newkey>` then, in a SEPARATE call, `--prune <oldkey>` (quote keys as "${P}:line-N" in zsh; `$P:l` is a zsh modifier).
- Do not pkill / kill processes by name. This machine is shared.

## Validation gate (SERIALIZED across agents — the host is overloaded)
1. `make validate-quick` while iterating.
2. COMMIT first (stage paths explicitly), then run the full gate under the shared lock, as a background command:
   ```
   LOCK=~/dev/worktree/issue-orchestrator/.coord/gate.lock
   until mkdir "$LOCK" 2>/dev/null; do sleep 30; done
   make validate-pr > <your-log> 2>&1; rc=$?; rmdir "$LOCK"; echo rc=$rc
   ```
   ALWAYS release the lock, even on failure. Never busy-wait on it.
3. If it fails, read `<worktree>/.issue-orchestrator/diagnostics/prepush/<sha>/validation-stdout.log`. A failure in an area you did not touch may be a load flake: re-run once; if it repeats, investigate and fix it (test failures are your responsibility).
4. Commit messages end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`. PR bodies end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)` and include `Closes #<n>`, Why / What changed / Tests sections, and the Final abstraction pass statement.

## Adversarial review loop (required)
- Review the REAL MERGE RESULT: `git worktree add --detach $HOME/dev/worktree/issue-orchestrator/review-<branch> origin/<branch>`, then in it `git merge --no-edit origin/main`. Refresh it (checkout --detach origin/<branch>; merge origin/main) before each round.
- Run codex in the background (10-60 min): `codex exec --sandbox read-only --cd <review-worktree> - < prompt.txt > out.txt 2>&1`. The verdict is the text after the last `tokens used` line.
- Prompt: the bug, the claimed fix, what to probe hardest; then append the contents of `review_output_format.txt` (same directory as this brief). From round 2 on, list every prior finding with its claimed fix and tell it to VERIFY each in the code.
- After EVERY round, post that round's result on the PR (`gh pr comment <pr> --body-file ...`): findings, what you changed, test evidence. Iterate until `VERDICT: APPROVED`. Validate each finding before acting — if one is wrong, explain why in the next prompt and in the PR comment rather than blindly complying. Re-run the serialized gate and push after each round's fixes.
- Never merge. Never push to main.

## Report back (your final message)
PR number/URL, final verdict and round count, the findings per round and how each was resolved, gate status at the final SHA, anything deferred (with a follow-up issue link), and any risk you or the reviewer could not close.

## FEATURE BRANCH (from 2026-09-27; overrides the base/merge rules above)
All tech-lead reliability work integrates on `tech-lead-reliability` (cut from main 72d207e), NOT main.
- Open your PR with `--base tech-lead-reliability`. For a PR already open against main: `gh pr edit <n> --base tech-lead-reliability`.
- Branch new work from `origin/tech-lead-reliability`. Review worktrees merge `origin/tech-lead-reliability`, not origin/main. The gate runs on your branch merged with the current feature-branch tip.
- The COORDINATOR (not you) merges approved sub-PRs into the feature branch with MERGE commits (never squash), then merges main into the feature branch periodically. Never push to the feature branch or to main yourself.
- If the feature branch moves under you, `git merge origin/tech-lead-reliability` into your branch (don't rebase a published branch), re-gate, and push.
- Your report must say "APPROVED at <sha>, base tech-lead-reliability" so the coordinator can merge.

## Gate lock: never remove a lock you did not create (added 2026-09-27)
The lock is also held by the COORDINATOR's live exam runs (`make test-tech-lead-exam`), not only by `make validate-pr`. "No validate-pr is running" does NOT mean the lock is stale. Never `rmdir` a lock you did not take. If you think it's stale, check for `make test-tech-lead-exam` and `make validate-pr` processes, and if either exists, wait. Otherwise report it to the coordinator instead of removing it.
