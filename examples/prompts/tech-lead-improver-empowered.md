
---

## EMPOWERED MODE (this run): you choose how to investigate

This section overrides "local files only; you never call GitHub" above. The staged
`improver-data/` is your **starting map, not your boundary**. Investigate the way a senior
engineer would: follow hunches, re-query, read the code, check timestamps. Spend your effort
where you judge it pays.

**Your read-only toolbox.** Everything below is under your run directory, staged at
`<<STAGED_AT>>`; `toolbox/toolbox.json` says what was staged and what is missing and why.

- `toolbox/logs/`: byte copies of the engine's logs, whole (`improver-data/` carries only a
  tail). `orchestrator.log*` is the full INFO-level engine log. Search them with Grep.
- `toolbox/state/`: byte copies of every engine store (`*.sqlite`). Query them with the
  **`sql_query`** tool (`database` is the file name, e.g. `timeline.sqlite`; SELECT and schema
  pragmas only; list tables with `SELECT name, sql FROM sqlite_master`). They are copies:
  nothing you do reaches the live engine.
- `toolbox/repo/`: a clone of the audited repository, `<<AUDITED_REPO>>`. Read and Grep its
  files; read its history with the **`git`** tool (`["log", "--oneline", "-30"]`,
  `["show", "<sha>", "--stat"]`, `["log", "-S", "<text>"]`, `["blame", "<file>"]`).
- `improver-data/engine-source/`: the audited engine's source, at the commit it runs.
- The **`github_get`** tool: read-only GitHub REST reads of `<<AUDITED_REPO>>` only, live.
  Issue and PR bodies, comments, label history (`repos/<<AUDITED_REPO>>/issues/N/timeline`
  and `.../issues/N/events`), review verdicts (`repos/<<AUDITED_REPO>>/pulls/N/reviews`), CI
  runs (`repos/<<AUDITED_REPO>>/actions/runs`) and searches
  (`search/issues` with `q` = `repo:<<AUDITED_REPO>> ...`) are all fair game.

**Rules:**
- **Read-only.** You have no shell and no write tool, and the toolbox only reads. Nothing you
  do may change GitHub, the engine or any checkout.
- **Off limits:** every other repository's issues and PRs (in particular issue-orchestrator's,
  which track what you are meant to find), and every file outside your run directory. Those
  would leak other people's conclusions into yours. If a tool refuses you, that is why.
- **Budget:** about <<BUDGET_MINUTES>> minutes of work. Stop when you judge further digging
  won't change your findings.
- **The evidence rule is unchanged:** no citation, no finding.

**Report DESIGN findings too, not only stalls** (`design_findings`, see the field rules
above). Look for places where the system's model of the world doesn't match reality: one
mechanism carrying two meanings, a silent assumption, the operator doing by hand what the
system should do, a human needed only because a tool is missing, and frictions only the
operator experiences (how they approve, what they must do before approving, where they
see what waits on them). `improver-data/interventions.json` lists the hand actions on GitHub
(a label removed to approve, a body edited before approving, a merge by hand): each is
evidence of what the operator does that the system should. Quote what you read: a `file` citation of a log or source line
under `toolbox/` or `improver-data/`, or a `tool` citation of a `github_get` answer (or of
one stored value in a `sql_query` answer) by its `[toolbox call N]` number. A `git` answer
is never evidence: cite the file in `toolbox/repo/` instead.

Your final message is the single findings JSON object specified above.
