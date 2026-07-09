# ADR 0030: GitHub-backed logical merge queue

**Status:** Proposed
**Date:** 2026-07-09
**Tracks:** Issue #6721
**Supersedes:** the "humans merge" clause of ADR-0005, only for repositories
that enable an IO-owned queue mode (see Merge Authority below)

## Context

GitHub Merge Queue solves a real integration problem: multiple individually
green PRs can still break `main` when merged in a different order or after the
base branch moves. issue-orchestrator needs similar semantics for repositories
where GitHub Merge Queue is unavailable, including private repositories outside
GitHub Enterprise Cloud.

**A merge-queue feature already ships.** `control/merge_queue_coordinator.py`
is the declared single owner of merge-queue policy — eligibility, the
stale/conflict classification, the enqueue decision, and failure routing —
running in the awaiting-merge discovery phase under the
Observer → Planner → ActionApplier contract. Its config section is live and
user-documented: `merge_queue.enabled`, `merge_queue.provider` (today
constrained to `github`), `merge_queue.enqueue_after`
(`code-reviewed` | `triage-reviewed`), and `merge_queue.failure_action`
(`rework` | `needs_human`), with `merge_queue.enqueued` / `merge_queue.failed`
trace events. This ADR extends that feature. It must not create a second
queue-policy owner beside it.

Existing decisions constrain the design:

- ADR-0013 says GitHub labels are the crash-safe source of lifecycle truth.
  A hidden local queue of unpublished work would violate this; if IO dies,
  operators must be able to recover merge state from GitHub.
- ADR-0020 separates quick validation from the deeper publish gate.
- ADR-0005 says humans merge: today no merge mutation exists anywhere in
  ports or adapters — the only queue write is `enqueuePullRequest`. An
  IO-performed merge is a deliberate, scoped change to that decision and is
  recorded explicitly below.
- ADR-0029 orders stack merges strictly: a stack successor's merge gate is
  blocked (`PREDECESSOR_NOT_MERGED`) until its predecessor merges. Queue
  ordering must respect that partial order.

## Decision

Extend the existing merge-queue feature into a user-facing **logical merge
queue** over PRs that are already published to GitHub.

`MergeQueueCoordinator` remains the single owner of merge-readiness policy.
New backends are modes inside the coordinator, not a parallel subsystem. The
`merge_queue.provider` enum grows from `github` to:

- `github`: delegate to GitHub Merge Queue (today's behavior, unchanged).
- `io_serial`: IO processes one queued PR at a time.
- `integration_branch`: future mode where IO validates a staged integration
  branch.
- `manual`: IO surfaces order/readiness, but a human performs the merge.

No new config key is introduced. `enqueue_after` keeps gating queue entry and
`failure_action` keeps routing queue failures in IO-owned modes; neither is
duplicated by new settings.

An item is in the logical merge queue only after it has an open,
ready-for-review GitHub PR. Draft PRs are not queue-eligible (GitHub cannot
merge a draft; per the existing flow, PRs leave draft when review passes).
The queue is a control layer over the existing "awaiting merge" phase, not a
new agent-work lifecycle phase and not a local-only pre-publish backlog.

The dashboard should continue to show items as **Awaiting Merge**. The queue is
shown as a drill-down/subview for ordering, readiness, and blocked reasons
inside that awaiting-merge set.

## Merge Authority

This section partially supersedes ADR-0005.

- In `manual` and `github` modes, ADR-0005 is unchanged: IO never merges.
- In IO-owned modes (`io_serial`, future `integration_branch`), the
  orchestrator merges using the GitHub App installation credential, through a
  typed repository-host operation, subject to the repository's branch
  protection (required checks stay required). Enabling an IO-owned mode is the
  operator's explicit opt-in to orchestrator-performed merges.
- Agent credential isolation is untouched: agents still hold no credentials
  and cannot merge. The only merge path is the orchestrator's, and it runs
  only after the queue validation gate below.
- If branch protection rejects the merge, the item becomes `merge-blocked`
  with an explanatory comment; IO does not retry around protection.

## GitHub State Model

The durable minimum state lives in GitHub:

- The PR exists and remains open and ready-for-review while queued.
- Queue membership and active/blocked state are represented with labels on the
  PR — in IO-owned modes only. In `github` mode, GitHub Merge Queue owns queue
  state and IO does not mirror it into labels (a mirror would be a second
  source of truth IO cannot keep consistent).
- Comments explain failures or operator-action requirements.
- IO may keep local caches for speed, but it must reconstruct queue state from
  GitHub after restart.

Labels and their lifecycle (decided, not left open):

| Label | Meaning | Added by | Removed by |
|-------|---------|----------|------------|
| `merge-queued` | PR is a queue member | IO, when the `enqueue_after` gate passes and the PR enters the queue | IO, on merge, on `needs-rework` exit, or on operator dequeue |
| `merge-active` | IO is rebasing, validating, or merging this PR now | IO, after acquiring the queue claim | IO, when processing ends (merged, blocked, held, or claim released) |
| `merge-blocked` | Queue processing cannot continue without rework or human action | IO, with a failure comment, routed per `failure_action` | IO, when the PR re-qualifies (e.g. rework completes and the `enqueue_after` gate passes again) |
| `merge-hold` | Operator intentionally paused queue processing for this PR | Operator only | Operator only |

`merge-queued` persists while `merge-active` is present — membership and
processing are separate facts, and crash recovery depends on membership
surviving a crash mid-processing. Recovery rule: `merge-active` without a live
queue claim is demoted — IO removes `merge-active`, keeps `merge-queued`, and
comments why. `merge-hold` always wins: IO never selects a held PR and releases
an active item at the next safe point if a hold appears mid-processing.

Existing labels keep their meanings. `code-reviewed` means the review gate has
passed. `needs-rework` removes a PR from the merge-ready path and routes it
back to rework (IO strips `merge-queued`/`merge-active` when this happens).
`pr-pending` remains the issue-side indication that a PR exists and is not
merged.

### Ordering

Queue order must be recoverable from GitHub-visible facts, cheaply:

1. **Stack constraint first.** Order is a linear extension of the ADR-0029
   stack partial order: a successor never precedes its predecessor, regardless
   of the facts below.
2. **Queue-entry marker comment.** When a PR enters the queue, IO posts a
   marker comment; its `created_at` arrives with normal comment fetches and is
   the primary ordering timestamp. Label-applied timestamps are *not* used for
   ordering: recovering them requires per-PR timeline-events calls, which the
   GitHub API discipline rules out as a steady-state cost.
3. **PR number** as the deterministic tiebreak and the fallback when the
   marker is missing.

A local database may cache the computed order, but it is not authoritative.

### Stack-aware processing

"Pick the next queued PR" consults the existing dependency merge gate
(`evaluate_merge_gate`). A successor whose predecessor has not merged is
**waiting**, not failed: it is never marked `merge-blocked` and never routed to
rework for that reason alone. The UI shows it as waiting on its predecessor.

## Claim Authority and Multi-Client Coordination

The logical merge queue must reuse and generalize IO's existing claim/lease
coordination instead of adding a separate queue lock system.

The software abstraction should be a resource-oriented **claim authority**. It
owns the claim protocol; GitHub remains the durable backing authority. Current
issue work uses this protocol to claim one issue before launching a session.
The logical merge queue should extend the same lower-level artifacts to typed
resources such as:

- `issue:<number>` for coding/rework session ownership.
- `pull_request:<number>` for queue item ownership.
- `merge_queue:<base_branch>` for optional queue-lane ownership. Resource keys
  must be encoded ref-safely (branch names contain `/`).

The claim authority should continue to use GitHub-backed compare-and-swap
leases. The existing `io:claimed` label pattern is useful for visibility, but
the authoritative ownership check must read the backing claim record, not just
a label.

Queue labels and claim labels have different roles:

- Queue labels (`merge-queued`, `merge-active`, `merge-blocked`, `merge-hold`)
  describe PR queue state.
- Claim records describe which IO client is temporarily allowed to mutate a
  queue resource.

This distinction matters because issue claims are scoped to active coding
sessions. Awaiting-merge PRs often have no active session, so the queue cannot
depend on the issue-session lease lookup path unchanged. IO-owned queue modes
must acquire a queue/PR claim before marking a PR active, rebasing, validating,
merging, or writing queue-state labels/comments.

Stale-claim policy is resource-specific. A stale issue-session claim may remain
blocked for human investigation, while an expired queue-item claim can usually
be cleared, commented, and requeued or retried according to queue policy.

## Validation Semantics

Logical queue processing adds a base-aware validation gate:

1. Agent completion validation answers whether the branch works in isolation.
2. PR/CI validation answers whether the submitted PR passed normal checks.
3. Queue validation answers whether the PR works against the current merge
   candidate: latest `main` plus earlier queued items for the chosen backend.

IO should cache queue validation by:

```text
branch_head_sha + base_candidate_sha + validation_config_digest
```

where the digest covers the validation command plus any configuration that
changes validation behavior, so "validation config changed" invalidates the
cache by construction rather than by convention. If no tuple field changed, IO
may reuse the result. If `main` moved, a predecessor merged, the branch was
rebased, or validation configuration changed, IO must validate again before
merging.

## Consequences

### Positive

- Private repositories without GitHub Merge Queue can still serialize
  integration and avoid racing individually-green PRs into `main`.
- One policy owner: `MergeQueueCoordinator` grows modes instead of gaining a
  sibling subsystem, so stale checks, review gates, and queue outcomes cannot
  drift across code paths.
- GitHub remains the durable state store; a restarted IO can reconstruct the
  queue from open PRs and labels.
- The UI has one stable product concept, "logical merge queue", regardless of
  backend mode.
- Queue validation is explicit and base-aware, reducing broken-main risk.

### Negative

- ADR-0005's "humans merge" invariant is deliberately narrowed: repositories
  that enable an IO-owned mode accept orchestrator-performed merges.
  Mitigations: explicit opt-in, GitHub App credential scoping, branch
  protection stays authoritative, and agents remain unable to merge.
- Some validation runs are intentionally repeated when the merge base changes.
- Labels cannot represent rich ordering by themselves; IO must derive order
  from the marker comment, stack constraints, and deterministic policy.
- The IO-owned queue backend needs careful recovery, stale-lock handling, and
  conflict/rework routing.

## Related

- ADR-0005: Enforce human merge and agent credential isolation (partially
  superseded by this ADR, as scoped above)
- ADR-0013: GitHub labels as crash-safe source of truth
- ADR-0016: Orchestrator as mediator
- ADR-0020: Quick and publish validation gates
- ADR-0029: Stacked work via typed dependency edges
