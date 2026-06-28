# ADR 0029: Stacked work via typed dependency edges

**Status:** Proposed
**Date:** 2026-06-28
**Milestone:** P1
**Tracks:** Issues #6594, #6595, #6596, #6597 (epic #6598)

## TL;DR

Stacked PR chains (C → B → A) put a human merge in the agent-work critical
path: today the orchestrator cannot start B until A is *closed*. We fix this
**not** by adding a parallel "stack workflow", but by giving the existing
dependency edge a *mode*. A **normal** edge keeps ADR-0009 behavior (wait for
the dependency to close). A new **stack** edge lets the dependent slice start
once its predecessor is validated, agent-reviewed, and has a usable branch —
while *merge* readiness stays strictly ordered. A single
**dependency gate report** answers work / review / publish / merge readiness so
the scheduler, session launch, publish, recovery, and UI consume one policy
instead of each re-deriving it.

## Context

The orchestrator already models inter-issue ordering with `Depends-on:`
references parsed in
[`domain/dependencies.py`](../../../src/issue_orchestrator/domain/dependencies.py).
`parse_dependency_refs()` extracts `#123`, `owner/repo#123`, and `M1-010`-style
references; `DependencyReport.runnable` is true only when every dependency is
`SATISFIED`, and a dependency is `SATISFIED` only when its issue is **CLOSED**.

That single CLOSED gate is correct for *independent* work but conflates two
distinct questions for *stacked* work:

1. **May the dependent slice's agent work begin?** For a stack, B only needs
   A's *branch* (validated and agent-reviewed) to exist so it can branch from
   it. It does not need A to be merged.
2. **May the dependent slice merge?** This *must* stay ordered: B cannot merge
   before A, because B's branch is based on A's branch.

Because both questions resolve through "is the dependency CLOSED?", a human
merge of A sits on the critical path before B's agent can even start. For a
three-deep stack (C → B → A) the human becomes a serializing bottleneck across
the whole chain, defeating the point of running agents in parallel.

Two existing decisions constrain the fix:

- **ADR-0005 (human merge & credential isolation).** Agents never merge; a
  human performs the merge. We preserve per-unit human review and merge. The
  goal is to remove the merge from the *agent-start* path, not to let agents
  merge.
- **ADR-0009 (dependency scoping).** Dependencies are same-milestone by default
  with an explicit cross-milestone escape hatch. A stack that legitimately
  spans milestones must not silently widen that policy for every edge.
- **ADR-0013 (labels as crash-safe truth).** Labels survive restarts, but they
  are orchestrator-authored intent, not authoritative facts about git/PR state.

## Decision

### 1. Stacking is a dependency *mode*, not a separate workflow

We model stacked work as a **typed dependency edge** with two modes:

- **Normal edge** — the existing `Depends-on:` semantics. The dependent issue
  waits until the dependency issue is **closed** (`DependencyState.SATISFIED`).
- **Stack edge** — a *predecessor* edge. The dependent slice may **start agent
  work** once its predecessor is **validated, agent-reviewed, and exposes a
  usable branch**. **Merge readiness remains ordered**: the dependent slice
  cannot merge until its predecessor has merged.

There is intentionally **no** standalone "stack" subsystem, state machine, or
scheduler. The design is only successful if stacking is expressible as
`(dependency edge) + (edge mode) + (gate report)`. Anything that requires a
parallel stack workflow is out of scope and a signal we took the wrong turn.

### 2. Git/PR facts are authoritative for stack correctness

Stack correctness is decided from **git and PR facts**, not labels:

- branch refs exist and are usable as a base,
- PR base/head are what the stack expects,
- ancestry is correct (the dependent branch descends from the predecessor),
- check/validation state is green,
- review freshness (the reviewed commit is still the head).

Labels (ADR-0013) may still drive lifecycle, recovery, and display, but they
**must not be the sole correctness source** for a stack decision. When labels
and git/PR facts disagree, git/PR facts win.

### 3. One dependency gate report answers four questions

A single **dependency gate report** — an extension of today's
`DependencyReport` — answers, per dependent slice:

| Gate | Question | Stack edge unblocks when… |
|------|----------|---------------------------|
| **work** | may agent work start? | predecessor validated + agent-reviewed + branch usable |
| **review** | may review start / is review fresh? | dependent's reviewed commit is current |
| **publish** | may a PR be created/updated? | predecessor branch exists to base on |
| **merge** | may this slice merge? | predecessor has merged (ordered) |

For a **normal** edge all four gates collapse to the existing rule: open until
the dependency closes. The scheduler, session launch, publish, recovery, and
UI **consume this one report** rather than each re-deriving stack policy. This
is the central anti-drift requirement: stack policy has exactly one owner.

### 4. Cross-milestone behavior stays bounded

ADR-0009's same-milestone-by-default rule **stays intact**. The only relaxation
is for **explicitly discoverable same-stack chains**: a chain whose edges are
declared as stack edges and resolvable as a single stack may span milestones.
We do not turn every dependency edge into a cross-milestone escape hatch.

## Implementation sequence

These issues are intentionally serialized with `Depends-on:` because the
current orchestrator cannot yet understand a `Stack-after:`/stack-edge marker —
the very capability #6594 introduces. Each implementation issue keeps exactly
one `agent:*` label so agent routing stays deterministic.

1. **#6594 — Add typed dependency edges and gate reports.** Introduce the edge
   mode (normal vs stack) at the parse/domain layer and produce the unified
   dependency gate report (work/review/publish/merge). Pure domain plus the
   git/PR fact inputs it consumes.
2. **#6595 — Use stack work gates in scheduler and session launch.** Make the
   scheduler and session launch consume the gate report's *work* gate so a
   stacked dependent can start before its predecessor merges.
3. **#6596 — Create stacked PR branch bases and enforce merge gates.** Base the
   dependent PR branch on the predecessor branch and enforce the *publish* and
   *merge* gates so merge ordering is preserved.
4. **#6597 — Surface stack dependency gates in the dashboard.** Render the gate
   report so operators can see why a stacked slice is (un)blocked at each gate.

This ADR is the **first PR** of the epic; it records the model and sequence
before any code lands.

## Rationale

- **One policy owner.** Routing every consumer through a single gate report is
  the same anti-drift move as ADR-0027 (one named resume state machine): the
  bug class we are avoiding is each call site keeping its own copy of "is this
  stack ready?" and drifting.
- **Facts over labels for correctness.** Stacks are fragile to rebases, force
  pushes, and stale reviews. Deriving correctness from git/PR facts (per
  ADR-0002/0003's "observe inbound truth" lineage) keeps a crashed-and-restarted
  orchestrator from trusting a stale label.
- **Preserves the human-merge contract.** ADR-0005 is untouched: humans still
  review and merge each unit. We only remove the *merge* from the
  *agent-start* path, not from the pipeline.
- **Bounded blast radius.** Reusing the dependency edge means no new lifecycle
  surface; ADR-0009 scoping is preserved except for explicit same-stack chains.

## Consequences

### Positive

- Dependent agent work parallelizes across a stack instead of serializing on
  human merges.
- Per-unit review and merge are preserved; the human is no longer a chain-wide
  bottleneck.
- Scheduler, launch, publish, recovery, and UI share one stack policy, so the
  gate logic cannot drift between them.

### Negative

- The dependency report grows from one boolean (`runnable`) to a four-gate
  report; consumers must ask the *right* gate.
- Correctness now depends on reading git/PR facts (ancestry, base/head, check
  and review freshness), which is more API/observation work than reading a
  label.
- Stacked branches can break on rebase/force-push of a predecessor; recovery
  must re-derive from facts rather than trusting labels.

## Follow-ups

- Implement the issue series #6594 → #6597 in order.
- Add tests at the gate-report boundary: normal edge collapses to the
  closed-only rule; stack edge unblocks *work* on validated+reviewed+branch but
  keeps *merge* ordered; stale-review and wrong-ancestry facts re-block.
- Confirm cross-milestone relaxation is limited to discoverable same-stack
  chains and does not weaken ADR-0009 for normal edges.

## Related

- ADR-0005 — human merge and agent credential isolation (preserved).
- ADR-0009 — dependency scoping (refined, not superseded).
- ADR-0013 — labels as crash-safe truth (labels assist; git/PR facts decide
  stack correctness).
- ADR-0027 — one named state machine owns a cross-cutting policy (pattern this
  ADR follows for the gate report).
