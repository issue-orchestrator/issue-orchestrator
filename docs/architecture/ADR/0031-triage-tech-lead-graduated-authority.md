# ADR 0031: Triage as a tech lead with graduated, config-scoped authority

**Status:** Proposed
**Date:** 2026-07-10 (amended 2026-07-11: §2 gated-issue surfacing, #6778)
**Milestone:** P1
**Tracks:** Issues #6760, #6761, #6762, #6763, #6764, #6778

## TL;DR

The triage facility was conceived as a tech lead — an agent that periodically
looks at groups of jobs, spots systemic problems ("five sessions are hanging
because of X"), and gets X fixed. What shipped is narrower and partly
disconnected: a batch PR-labeler whose findings go nowhere, plus a
failure-investigation path whose diagnosis evaporates on session exit. We fix
this **not** with a new subsystem but by giving triage the three organs it is
missing: an **output channel** (a typed decision artifact the orchestrator
validates and executes, mirroring the review-exchange contract), an
**observation surface** (a board-snapshot manifest of orchestrator state,
extending the existing PR-manifest pattern), and a **periodic trigger** (an
interval-driven health review). Authority is **graduated in config, per action
type**: the agent always proposes its full action set; configuration decides
which proposals the orchestrator executes and which it merely surfaces as
*would-have-done*. Trust becomes a dial the operator turns, not a code change.

## Context

Today triage has two triggers and no periodic behavior:

1. **Batch PR review.** When `triage_review_threshold` PRs carry the
   `code-reviewed` label, the planner creates a "Batch Review" issue
   (`fact_gatherer.gather_triage_facts()` →
   `planner._plan_triage_issue_creation()`). The session receives a manifest of
   pre-downloaded PR diffs/metadata (`TriageManifestBuilder` +
   `ManifestDownloader` port). On completion the orchestrator performs exactly
   one act: adding `triage-reviewed`/`triage-failed` to the manifest PRs
   (`completion_action_planner._generate_triage_actions()`). The findings the
   prompt asks for — "identify patterns and systemic issues" — have no channel:
   no comment, no issue, no report artifact. The prompt explicitly forbids the
   agent from creating them itself.

2. **Failure investigation.** Failed/timed-out sessions are queued
   (`planner._plan_discovered_failures()`, gated on
   `triage_review_on_failure`) and launched as triage sessions. These have no
   manifest, so completion produces **nothing** orchestrator-side. The
   diagnosis is write-only.

Three further defects block building on this foundation:

- `TriageWorkflow`'s batch-trigger engine (`should_trigger_batch_triage`, the
  30-minute cooldown, `BatchTriageDecision`) is dead code — exercised only by
  unit tests, never called in production. `TRIAGE_BATCH_TRIGGERED` never fires.
- Three prompt variants disagree on data source, permissions, and completion
  verb; the wizard-generated one promises orchestrator behavior (comment
  posting, label flips) that `_generate_triage_actions()` does not perform.
- The agent's inputs cannot support the vision. It sees PR diffs or a one-line
  failure title — never session states/ages, blocked-queue reasons, timeline
  events, or logs, which is where hang-class and infrastructure-class problems
  actually show up.

Existing decisions constrain the fix:

- **Agent intent, orchestrator authority.** Agents express intent in records
  the orchestrator validates as untrusted input; agents never push, merge,
  mutate labels, or create issues directly. Any tech-lead "action" must be a
  *proposal* executed by the orchestrator.
- **ADR-0013 (labels as crash-safe truth)** — triage state transitions remain
  label-driven and restart-safe.
- **The review-exchange artifact contract** (review-report.md +
  review-decision.json, ADR-0027 lineage) already established the house
  pattern for "agent writes paired human/machine artifacts; the JSON is the
  authoritative contract." We reuse it rather than inventing a second shape.
- **Issues drive work.** The operator's actuator is the issue tracker; a
  tech-lead agent whose primary output is *well-formed issues* feeds its
  findings back into the same orchestration loop that fixes them.

## Decision

### 1. Output channel: a typed triage decision artifact

Triage sessions complete by writing a paired artifact set, mirroring the
review exchange:

- **`triage-report.md`** — the human-readable tech-lead report.
- **`triage-decision.json`** — the authoritative contract, validated as
  untrusted input at completion time.

The decision carries **typed findings** (each with a classification —
`infra | task | agent | systemic` — and evidence references into the inputs it
was given) and **typed proposed actions**:

| Action type | Meaning | Default authority |
|---|---|---|
| `post_comment` | Diagnosis comment on an issue/PR | `execute` |
| `create_issue` | File a follow-up issue (labels, milestone per `triage:` config) | `execute` |
| `escalate_to_human` | Route to the needs-human surface | `execute` (floor: cannot be disabled) |
| `flag_pattern` | Open/append a durable pattern case-file issue for a cross-job pattern (amended by #6781); requires a `pattern_signature` | `execute` |
| `reset_retry` | Reset-and-retry an issue from scratch (executor wired — #6764 first slice) | `propose` |
| `kill_hung_session` | Terminate a stuck session (executor not wired yet — #6764) | `propose` |

The orchestrator parses the decision on session completion, applies the
authority filter (§2), executes allowed actions through the existing
action/applier vocabulary, and surfaces the rest. Malformed or contract-violating
decisions fail loudly: the session is marked triage-failed and the parse error
is preserved. Completion verbs stay `coding-done completed|blocked` — the
artifact, not the CLI flags, carries the structure.

### 2. Graduated authority lives in configuration, per action type

```yaml
triage:
  authority:
    post_comment: execute        # execute | propose
    create_issue: execute
    flag_pattern: execute
    reset_retry: propose         # shadow mode
    kill_hung_session: propose
```

Semantics:

- The agent **always proposes its full action set**; prompts do not change as
  trust grows. Graduation is flipping `propose` → `execute` in config (a
  settings-UI toggle, since these keys are in the settings schema).
- `propose` on `post_comment`/`flag_pattern` is **shadow mode**: the action is
  recorded visibly — in the report, as a structured event, and on the
  escalation surface — as *would-have-done*, giving the operator an audit
  trail to compare against their own judgment before granting authority.
- **Gated issues (amended by #6778).** Consequential proposals surface as
  *gated GitHub issues* instead of shadow records: `create_issue` proposals
  under `propose` authority are created carrying the `proposed-triage` label,
  and act-level proposals (`reset_retry` under `propose`;
  `kill_hung_session` always, until its direct tier is wired) become gated
  proposal issues. **Removing `proposed-triage` is per-instance approval**:
  a proposed work issue flows into normal scheduling; an act-level proposal
  triggers execution of the **stored op** recorded orchestrator-side at
  creation (authority-store pattern, keyed by issue number, create-once).
  The issue body is human documentation only and is never re-parsed as a
  command — what the approver read and delabeled is exactly what runs. The
  scheduler's blocking-label layer excludes gate-labeled issues from pickup,
  and `proposed-triage` joins the protected/orchestrator-owned label family
  (agents cannot propose or strip it). Ledger hygiene: one open proposal per
  (op, target); re-proposals comment on the existing issue. Ops execute at
  most once — the op row is discarded after terminal handling (outcome
  comment + close on execution, or stale-downgrade comment + close).
  Per-instance approval and config-level trust coexist.
- **Durable pattern case files (amended by #6781).** `flag_pattern` under
  `execute` is no longer event-only. Each flag_pattern action carries a
  required `pattern_signature` (a short stable slug; a decision without one is
  rejected). The orchestrator keeps a durable case-file ledger keyed by that
  signature: the first time a signature is observed it opens a **pattern
  case-file issue** (create-once, keyed by signature), and every repeat
  observation appends an **evidence comment** to that same case file rather
  than opening a new one. Evidence therefore *accrues* on one issue per
  pattern, and the open case files are projected into the board snapshot (§3)
  and the local triage board so the periodic health review (§4) can mine
  accumulated cross-job evidence. The `mode="pattern"` trace event still fires.
  Under `propose`, `flag_pattern` stays a shadow *would-have-done* record and
  opens no case file.
- Per-action flags, not a level scale: trust is not linear. An operator may
  trust issue-filing for months before trusting session-killing.
- Fail-safe: anything that mutates orchestrator runtime state defaults to
  `propose`. Setting `execute` on an action type whose executor is not yet
  wired (§5) is a **startup configuration error**, never a silent no-op.
  (`reset_retry` is wired — the #6764 first slice — so `execute` on it is
  honored; `kill_hung_session` remains startup-rejected.)
- Execution-time re-validation: act-level proposals are executed only if their
  recorded preconditions still hold (the board may have moved since the agent
  wrote the decision); otherwise they downgrade to surfaced proposals with an
  event.

### 3. Observation surface: the board-snapshot manifest

The manifest pattern extends beyond PR diffs. Triage sessions receive, in
their `triage-data/` directory, a typed **board snapshot**:

- active sessions (type, state, age, issue, terminal),
- pending/blocked queues with reasons,
- recent failures with paths to session artifacts and failure diagnoses,
- recent timeline extracts for affected issues,
- an orchestrator log tail.

All board data is local state — no new GitHub API traffic. Failure
investigation sessions, which today receive nothing, get the snapshot scoped
to the failed issue plus board context; batch sessions get it alongside the PR
manifest. The canonical prompt documents the layout; the agent stays
sandbox-compatible (reads local files, never queries GitHub).

### 4. Periodic trigger: the health review

`triage.health_review.interval_minutes` (absent/0 = disabled) drives a
planner-side trigger: when the interval elapses and no health review is active
or pending, queue a triage session of flavor `health-review` carrying the
board snapshot. The last-run marker is persisted so restarts do not
double-fire. Capacity/pause gating reuses `TriageWorkflow.should_launch_triage()`.
Health-review completions flow through the decision artifact — there is no PR
manifest to label.

### 5. Sequencing and scope boundaries

Hygiene precedes construction: the dead batch-trigger engine and its
false-confidence tests are deleted, the never-emitted `TRIAGE_BATCH_TRIGGERED`
event is removed, the missing triage keys join the settings schema, and the three
prompt variants collapse to one manifest-based contract (#6760). The decision
artifact and authority filter land next (#6761), then the board snapshot
(#6762), then the periodic trigger (#6763). Act-level executor wiring
(`reset_retry`, `kill_hung_session`) is deliberately last (#6764): the
vocabulary and shadow-mode surfacing ship first, so operators accumulate
would-have-done evidence before any execute flag exists to flip.

Non-goals: the triage agent never edits code, never pushes, never merges, and
never mutates labels or GitHub state directly — its writes are the two
artifact files; everything else is orchestrator-executed proposal. Dashboard
work is limited to surfacing the report/decision through the existing
issue-artifact pattern.

## Consequences

- Failure investigation becomes useful immediately: every failed session can
  end in a diagnosis comment on its issue, classified and evidence-linked —
  the first concrete slice of operator workload actually replaced.
- The operator's trust boundary is explicit, inspectable, and reversible; an
  incident response can be "set everything back to propose" in one config
  edit.
- The decision artifact adds a second consumer of the paired-artifact pattern,
  pressuring it toward a shared owner abstraction if a third appears
  (retrospective review is the likely candidate).
- Shadow mode produces structured would-have-done data; if we later want to
  score the agent's judgment against operator actions, the record already
  exists.
- Deleting the dead cooldown machinery removes the misleading tests; the
  periodic trigger re-introduces time-based logic wired and tested honestly.
