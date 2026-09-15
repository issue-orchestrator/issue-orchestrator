# Case-File Reconciliation Runbook (#6989, #7240)

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

This runbook covers that evidence backlog and reviewed lifecycle cleanup for
case files whose underlying work has shipped, moved, or needs a human decision.

## The mechanism

```bash
issue-orchestrator reconcile-case-files --plan <plan.yaml>          # dry run
issue-orchestrator reconcile-case-files --plan <plan.yaml> --apply  # execute
```

The command accepts two strict, checked-in plan forms. It is **dry-run by
default**.

An evidence plan contains `clusters` and folds older duplicate issues onto one
tracker. It plans in two phases:

1. **Evidence.** Each cluster's signature gets (or joins) one pattern case file,
   and every accumulated duplicate lands there as an observation carrying that
   plan author's summary and a link to the original issue. Original titles,
   bodies and comments remain on those issues; they are not copied.
2. **Closure.** After evidence lands, the duplicate receives an explanation
   naming its tracker and case file before it closes. A failed explanation
   leaves it open; a failed close retries without repeating an already recorded
   explanation.

If phase 1 fails for any action, phase 2 does not run — the command refuses to
close an issue whose evidence went nowhere.

A lifecycle plan contains `repository`, `recorded_at`, and `outcomes`. It must
name every signature and exact issue mapping in the shared registry snapshot.
The dry run refuses a partial, stale, or wrong-repository plan, and refuses one
whose transitions are not admissible at all — a signature already terminal under
a different transition, a transition identity reused with a changed payload, or
an interrupted retirement whose pending comment is not the one this plan would
render — before any write. Preflight asks the same rules the reserving
compare-and-swap asks, so an outcome it admits is one the apply can perform. The
whole plan is admitted before any of it is applied, so a bad row at position 40
stops the command instead of stopping it after 39 case files have already been
commented on and closed.

A plan is rejected at load, before anything is composed, unless every outcome
can build its lifecycle transition — which means `recorded_at` must be
timezone-aware ISO-8601, not merely parseable. That is a plan error (exit 2),
not a refusal.

`--apply` selects the composition, not merely what it does afterwards. Without
it the command holds read-only shared authority: it does not open, create, or
migrate the local SQLite authority store, does not publish a rolling-upgrade
seed, and cannot mirror shared rows over richer local evidence or discard a
pending create intent while reading. Every stop it can make — including a shared
authority read that fails for transport, authentication, or registry reasons —
is a nonzero exit with an explanation, never a traceback.
Each outcome is one of `active`, `needs_human`, `shipped`, `superseded`,
`invalid`, or `declined`. Terminal outcomes post an auditable explanation and
evidence to the case file before closing it; nonterminal outcomes remain open
with their reviewed state recorded in the shared registry. A retry resumes the
same stable transition even when its wall clock has advanced.

Retiring a case file also removes its signature from the finding-promotion
lane, **from the moment the retirement is admitted** rather than when it
finishes. Those are not the same instant: a reservation is followed by a
comment, a remote close, and a final compare-and-swap, and a process that stops
in between leaves shared authority holding a durable terminal intent. The
registry's own decision — settled terminal, or terminal retirement in flight —
is projected durably onto the local pattern ledger promotion eligibility already
reads, so it survives a restart, a resynchronize, and a cold second client, and
later evidence keeps accruing on the case file without reviving it. Without
that, applying this backlog would close 42 case files and let the next promotion
tick re-file work for the code-fix signatures among them, which have evidence
above the threshold and no promotion row.

Because a nonterminal outcome *asserts* the case file stays open, its write is
granted only against an issue GitHub still reports as open, read fresh at the
moment of the write. A human closing a case file between review and apply
therefore halts that outcome instead of leaving durable authority saying
`active` about a closed issue — the pause label alone cannot catch this, since
a closed issue has perfectly readable labels. Terminal outcomes do not carry
that requirement: closing is idempotent, and requiring `open` would stop the
command from finishing its own interrupted retirement.

### Properties you can rely on

- **Bounded.** It touches exactly the issue numbers written in the plan file. It
  discovers nothing, searches nothing, and closes nothing it was not told about
  by name. A cluster's `tracker` is never closed. Every mutation it plans is
  fail-closed on the pause label: an issue carrying `io:needs-reconcile` halts
  the run instead of being written to, and a nonterminal outcome additionally
  requires its case file to still be open. Both facts come from one fresh,
  uncached read; a read that fails is unknown, which fails closed exactly like
  a violation.
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

Lifecycle plans use the same immutable `plan_id` rule:

```yaml
plan_id: "7240-porchpin-case-files-2026-09"
repository: porchpin/porchpin
recorded_at: "2026-09-10T23:15:00+00:00"
outcomes:
  - signature: example-recurring-class
    issue: 123
    disposition: shipped
    expected_revision: "<SHA-256 review revision from the dry-run snapshot>"
    reason: The concrete reason this case file is complete.
    evidence:
      - https://github.com/porchpin/porchpin/pull/456
```

`expected_revision` binds the decision to the exact evidence, classification,
and lifecycle history that was reviewed. If any of those facts change before
apply, the whole plan is refused and must be regenerated and reviewed. Do not
trim a lifecycle plan to the open issues shown by GitHub search. The
shared registry can contain already-closed case files, and complete coverage is
what prevents an unreviewed historical entry from disappearing during cleanup.

Take every revision from the **shared registry as the apply will see it**, which
is not always what a local SQLite read shows. The two diverge on a repository
whose rows have not been published to shared authority yet: the first apply
seeds them, and seeding pads any legacy row whose `observation_count` exceeds
its stored observation identities with synthetic `legacy:` identities — which
are themselves part of the revision.

That splits into two cases, and a plan must be generated from the right one:

- **Already-shared registry** (an engine has run against this repo): the dry run
  reports it and is the source to use. The dry run reads shared authority
  directly and never opens the local SQLite replica, so a local read is a
  separate source that disagrees whenever this client has not synchronized.
- **Not yet seeded**: the dry run cannot preview it at all, because it refuses
  to write and therefore refuses to seed — it reports every planned signature as
  `unknown`. Generate the revisions from the seeded projection the first apply
  will publish (local evidence rows padded to `observation_count`).

A revision taken from the wrong side of that migration is refused as stale. That
is the correct outcome, but it costs a review cycle to rediscover, so record
which case a plan was generated under alongside its `plan_id`.

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

## Reviewed Tech Lead lifecycle backlog

Two checked-in plans classify the full durable registry as reviewed for #7240:

| Plan | Registry entries | Open `Pattern case file:` issues at review time |
|---|---:|---:|
| `tech-lead-case-lifecycle-issue-orchestrator-2026-09.yaml` | 13 | 13 |
| `tech-lead-case-lifecycle-porchpin-2026-09.yaml` | 53 | 49 |

The four extra Porchpin entries are already-closed case files retained by the
registry. They stay in the plan so the reconciliation is complete and
repeatable. Dry-run and apply each plan from the repository named inside it;
an absolute plan path is fine when operating Porchpin from an
issue-orchestrator checkout.

Across both plans: 66 reviewed outcomes, 43 terminal, 23 retained as active or
needs-human — a total `tests/unit/test_checked_in_reconciliation_plans.py`
derives from the plan files rather than trusting this sentence.

### Keeping a reviewed plan fresh

A plan is a snapshot of a judgement. The apply is protected against the
registry moving underneath it (`expected_revision`, whole-plan admission), but
nothing re-checks the reasoning a human wrote, and the gap between review and
merge is where that goes stale. Porchpin
`tech-lead-batch-manifest-diff-fetch-blocked-by-gh-guard` was published saying
its fix "remains in open PR #7238 and must merge before retirement" on a branch
whose own merge base already contained that merge; it is `shipped` here now.

Two halves, split by what can actually be known where:

- **Enforced in the unit suite, deterministically.** A retained (`active` or
  `needs_human`) outcome's `reason` may not assert the STATE of a numbered
  GitHub item — no `#1234`, no `/pull/` or `/issues/` link. Such a claim decays,
  and no part of the apply re-checks it. `reason` carries the durable claim;
  `evidence` carries the links and is deliberately not scanned, so an outcome
  that legitimately stays retained alongside related merged work says why in
  prose and cites the work under `evidence` — as
  `review-exchange-restart-rederives-reviewer-verdict` does with merged PR
  #7141. The suite also pins the corrected Porchpin outcome terminal so that
  specific regression cannot return.
- **Yours, before you publish a revised plan.** Re-check every retained outcome
  against GitHub: tracker still open, no fix merged since. A test cannot do
  this. "Is this pull request merged?" is a GitHub question, and these plans
  span two repositories, so a bare number cannot even be attributed to one of
  them. An earlier version of the guard tried to answer it from this
  repository's local history and was worse than nothing: CI checks out a single
  commit, so it saw no history and passed vacuously in the one environment that
  gates publication.

Both plans were reconfirmed on 2026-09-11 against their repositories'
`.issue-orchestrator/state/tech_lead_authority.sqlite`, each under the
projection named in its own header — issue-orchestrator against the shared
projection (13/13) and Porchpin against the seeded projection (53/53). The
Porchpin decision set was then re-reviewed on 2026-09-12, after PR #7238
merged, and its plan-wide `recorded_at` advanced past that merge.

That last part is the rule, not a detail of one edit: **when a re-review
changes an outcome on the strength of something that has since happened,
advance the plan's `recorded_at` to the re-review time.** `recorded_at` is not
plan prose — it rides into the `CaseFileLifecycleTransition` the apply writes,
so leaving it behind makes durable authority claim a decision predates the fact
it records. Correcting it before publication is safe rather than a second
decision: the transition identity is `plan_id:signature` and
`same_intent` deliberately excludes `recorded_at`, so an already-applied plan
still replays to its first reservation. Registry revisions are unaffected
either way — a review revision fingerprints the registry's settled facts, not
the plan's decision about them.

One ambiguity is carried deliberately rather than resolved: two
issue-orchestrator rows (`review-exchange-coder-no-completion` #6913 and
`stuck-sweep-ghost-failure-reinjection` #6927) hold an `observation_count` one
higher than their stored observation identities, so the two projections disagree
for them. Whether their plan revisions hold depends on whether shared authority
already carries those signatures, which cannot be read from a checkout. If it
does not, the apply refuses exactly those two outcomes as stale and they are
regenerated — the fail-closed path, which is why this is safe to leave to the
operator rather than guessed at here.

PR #7234 keeps every surviving active or needs-human case file on the dedicated
Tech Lead surface without repeating it in the general Blocked work lane.
