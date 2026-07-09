# ADR 0030: GitHub-backed logical merge queue

**Status:** Proposed
**Date:** 2026-07-09
**Tracks:** Issue #6721

## Context

GitHub Merge Queue solves a real integration problem: multiple individually
green PRs can still break `main` when merged in a different order or after the
base branch moves. issue-orchestrator needs similar semantics for repositories
where GitHub Merge Queue is unavailable, including private repositories outside
GitHub Enterprise Cloud.

Two existing decisions constrain the design:

- ADR-0013 says GitHub labels are the crash-safe source of lifecycle truth.
- ADR-0020 separates quick validation from the deeper publish gate.

A hidden local queue of unpublished work would violate those constraints. If
IO dies, operators must be able to recover merge state from GitHub.

## Decision

Introduce a user-facing **logical merge queue** over PRs that are already
published to GitHub.

An item is in the logical merge queue only after it has an open GitHub PR. The
queue is a control layer over the existing "awaiting merge" phase, not a new
agent-work lifecycle phase and not a local-only pre-publish backlog.

The logical queue has backend modes:

- `github`: delegate to GitHub Merge Queue when available.
- `io_serial`: IO processes one queued PR at a time.
- `integration_branch`: future mode where IO validates a staged integration
  branch.
- `manual`: IO surfaces order/readiness, but a human performs the merge.

The dashboard should continue to show items as **Awaiting Merge**. The queue is
shown as a drill-down/subview for ordering, readiness, and blocked reasons
inside that awaiting-merge set.

## GitHub State Model

The durable minimum state lives in GitHub:

- The PR exists and remains open while queued.
- Queue membership and active/blocked state are represented with labels on the
  PR.
- Comments explain failures or operator-action requirements.
- IO may keep local caches for speed, but it must reconstruct queue state from
  GitHub after restart.

Initial labels:

| Label | Meaning |
|-------|---------|
| `merge-queued` | PR is eligible for logical merge queue processing |
| `merge-active` | IO is currently rebasing, validating, or merging this PR |
| `merge-blocked` | Queue processing cannot continue without rework or human action |
| `merge-hold` | Operator intentionally paused queue processing for this PR |

Existing labels keep their meanings. `code-reviewed` means the review gate has
passed. `needs-rework` removes a PR from the merge-ready path and routes it
back to rework. `pr-pending` remains the issue-side indication that a PR exists
and is not merged.

Ordering must be recoverable from GitHub-visible facts: queue label timestamp
when available, PR creation time, review/approval timestamp, priority labels,
issue number, or a deterministic configured policy. A local database may cache
the computed order, but it is not authoritative.

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
- `merge_queue:<base_branch>` for optional queue-lane ownership.

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
branch_head_sha + base_candidate_sha + validation_command
```

If neither branch head nor merge candidate changed, IO may reuse the result.
If `main` moved, a predecessor merged, the branch was rebased, or validation
configuration changed, IO must validate again before merging.

## Consequences

### Positive

- Private repositories without GitHub Merge Queue can still serialize
  integration and avoid racing individually-green PRs into `main`.
- GitHub remains the durable state store; a restarted IO can reconstruct the
  queue from open PRs and labels.
- The UI has one stable product concept, "logical merge queue", regardless of
  backend mode.
- Queue validation is explicit and base-aware, reducing broken-main risk.

### Negative

- Some validation runs are intentionally repeated when the merge base changes.
- Labels cannot represent rich ordering by themselves; IO must derive order
  from GitHub-visible facts and deterministic policy.
- The IO-owned queue backend needs careful recovery, stale-lock handling, and
  conflict/rework routing.

## Related

- ADR-0013: GitHub labels as crash-safe source of truth
- ADR-0016: Orchestrator as mediator
- ADR-0020: Quick and publish validation gates
- ADR-0029: Stacked work via typed dependency edges
