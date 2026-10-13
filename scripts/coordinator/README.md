# Coordinator scripts

Operational scripts the coordinator uses to run io's development loop and porchpin's engine. They were local-only (`~/dev/worktree/issue-orchestrator/.coord/`); they live here so they have history and review. Runtime artifacts (locks, logs, exam output) stay in `COORD_DIR` (default `~/dev/worktree/issue-orchestrator/.coord`), never in git.

| Script | What it does |
|---|---|
| `merge_into_improver.sh <pr>:<sha>` | Merge an approved sub-PR into the integration branch (`tech-lead-improver`): waits for `validate-fast` by name, merge commit, full gate on the merge result, push; aborts a conflicted merge. Holds `gate.lock`. |
| `integration_merger.sh` | Loop (`HOURS=24`): merges ready porchpin PRs into porchpin's `integration`; skips PRs whose issue carries a standing ruling (checked by hand). |
| `merge_into_integration.sh <pr>:<sha>` | One porchpin PR into `integration`. |
| `exam_all.sh <sha>` | Full tech-lead exam (A–J, U) against a commit, under `exam.lock`. |
| `deploy_porchpin.sh <sha> [reason]` | Redeploy the Control Center + porchpin engine at the first idle moment; re-checks the commit right before acting; starts the CC detached with no `CLAUDE*`/`PYTHONPATH` from the caller (#9293). |

`BRIEF.md` is the brief coding agents follow (integration effort = label `integrate:<branch>`, gate/exam locks, review loop).

The io-on-io effort engine's config is `io-effort-engine.main.yaml` here; install it in the engine's full clone as `.issue-orchestrator/config/modes/next/main.yaml` (CLI-started detached with a clean env until #9242).

**Deliberately local:** each engine's runtime state (`.issue-orchestrator/state/`: escrow, SQLite) — operator decision 2026-10-08.

Fix forward: these are meant to be replaced by io itself (integration mode #9098, a deploy command), one script at a time.
