# Case-File Reconciliation Runbook (#6989)

Folding an **already-accumulated** duplicate cluster onto its durable pattern
case file.

## When you need this

The tech lead used to have exactly one non-filing route for a `create_issue`
proposal it recognized as a duplicate, and that route required the cited issue
to be inside the session's launch comment grant. A re-sighting of a *standing*
problem — a tracker the session does not own — could never take it, so every
daily re-verification of a known problem became a new open issue.

`control/tech_lead_observation_routing.py` closed that hole: an agent-cited
duplicate now accrues to the pattern case-file ledger instead of minting an
issue. That fix is forward-looking. It cannot retro-collapse the clusters that
had already piled up, and it cannot register a recurring class nobody ever
flagged.

This runbook is for that backlog.

## The mechanism

```bash
issue-orchestrator reconcile-case-files --plan <plan.yaml>          # dry run
issue-orchestrator reconcile-case-files --plan <plan.yaml> --apply  # execute
```

The command is **dry-run by default**. It plans in two phases:

1. **Evidence.** Each cluster's signature gets (or joins) one pattern case file,
   and every accumulated duplicate lands there as an observation carrying that
   issue's contents and why it was a re-sighting.
2. **Closure.** Only after its evidence has landed is a duplicate closed, with a
   comment naming both the tracker that owns the work and the case file that now
   holds the evidence.

If phase 1 fails for any action, phase 2 does not run — the command refuses to
close an issue whose evidence went nowhere.

### Properties you can rely on

- **Bounded.** It touches exactly the issue numbers written in the plan file. It
  discovers nothing, searches nothing, and closes nothing it was not told about
  by name. A cluster's `tracker` is never closed. Every mutation it plans is
  fail-closed on the pause label: an issue carrying `io:needs-reconcile` halts
  the run instead of being written to.
- **Idempotent, with one named exception.** The case file is create-once by
  signature (ledger row + remote marker recovery) and each observation is
  create-once at an identity derived from `plan_id` + the entry's id, so a
  re-run re-plans the same appends and the store skips them without commenting.
  A partial run is safe to repeat: it applies only what did not land.

  The exception is closure, which is gated on "still open" rather than on a
  durable record of the fold. **An issue reopened while it is still listed in
  the plan will be folded and closed again on the next run.** Disagreeing with a
  fold means editing the plan, not just reopening — which is what the closure
  comment tells whoever reads it.
- **Establishes nothing.** A reconciliation entry is an evidence-only sighting —
  no `fix_class`, no `area`, no `diagnosis`. A signature registered this way is
  a ledger plus an evidence trail and stays **unclassified**, so backfilled
  evidence can never make a class promotable, pick the repo a promotion routes
  to, or become what a promotion is filed on. That stays with a reviewed
  `flag_pattern`, which is free to classify the signature later.

The plan is executed through the ordinary `ActionApplier` using the same typed
actions the live lane emits, so there is no second policy for how evidence lands
on a case file.

## Writing a plan

Plans are checked in and reviewed like code — that is the point, since a plan
closes issues by number.

```yaml
plan_id: "6989-standing-problems-2026-08"   # IMMUTABLE once applied
clusters:
  - signature: search-api-budget-exhaustion  # the durable ledger key
    tracker: 6928                            # the issue that owns the WORK
    summary: >-
      What the recurring class is, in your words. Becomes the case file's
      first observation.
    duplicates:
      - issue: 6966
        note: >-
          Why this issue is a re-sighting rather than distinct work. Kept
          verbatim on the case file, so the fold stays reviewable after the
          issue is closed.
```

`duplicates` may be omitted: a cluster with none is a pure **registration** of a
recurring class, which is a legitimate use on its own.

The loader is strict and fails the whole run rather than applying part of a
plan. It rejects unknown keys, non-positive issue numbers, a signature repeated
across clusters, an issue listed as a duplicate twice, and an issue that is both
a duplicate and some cluster's tracker.

`plan_id` is load-bearing: it is the first component of every observation
identity the plan writes. **Do not edit it after applying** — changing it
re-posts every observation the plan already landed. A later backlog gets a new
plan file with its own id.

## Running it

1. Stop the engine. The command holds the repo lock for its whole lifecycle
   because it mutates the pattern ledger a running orchestrator also owns.
2. Dry-run and read every planned action. On a **first** run the closure list is
   necessarily empty — a closure can only be planned once its case file exists,
   and this run is what creates it — so the command prints those duplicates
   under `deferred until their case file exists`. That list is what `--apply`
   will close.
3. Re-run with `--apply`.
4. Confirm: the duplicates are closed and each names its case file; the case
   files carry one observation per folded issue.

```bash
gh issue list --state open --search '"Pattern case file:" in:title'
```

A run that reports `Reconciliation halted` did not match the board it expected —
usually a paused (`io:needs-reconcile`) issue. Resolve that state and re-run;
nothing partial is lost.

### Keeping future sightings on the case file you just registered

Registering a signature does not by itself route future sightings to it. When
the tech lead cites a duplicate without naming a `pattern_signature`, the
forward lane derives one from the cited issue (`duplicate-of-#<n>`), which would
open a *second* ledger for the same class. The tech-lead prompt asks the agent
to name the recurring class; the case files' titles carry the signature and
appear in the board snapshot, which is how it learns the existing names. If you
see a `duplicate-of-#<n>` case file for a class you registered here, that is the
signal to fold it in with a follow-up plan.

## This repository's backlog

`repo-specific/reconciliation/standing-problems-2026-08.yaml` carries the three
clusters #6989 names:

| Signature | Tracker | Folded in |
|---|---|---|
| `search-api-budget-exhaustion` | #6928 | #6966, #6977 |
| `stranded-branch-recovery` | #6983 | #6959, #6970, #6973 |
| `e2e-suite-chronic-red` | #6918 | #6961, #6978 |

The fourth recurring class #6989 names — "exchange-timeout-strands-validated-
work" — needs no entry: it was registered organically after #6989 was filed, as
case file #7012 `review-exchange-max-rounds-strands-validated-work` (promoted to
#7018).

Applying that plan is an **operator action**, not part of the code change:
agents report intent and never write to GitHub. #6989 stays open until it has
been run.
