---
name: review-workflow
description: Understand the code review pipeline, triage batch reviews, failure investigation, and rework cycles. Use when working on review labels, code review agents, triage configuration, or the needs-rework flow.
---

# Review Workflow

This skill provides context for the multi-stage review pipeline.

## When to Use

- Working on code review pipeline
- Configuring triage batch reviews
- Understanding rework cycles
- Working with review-related labels
- Failure investigation flow

## Key Resources

Read this file for context:
- [Review Workflow](../../../docs/development/REVIEW_WORKFLOW.md) - Full workflow documentation

## Review Checklist: Cross-Cutting Policy

When reviewing changes that touch review/rework/triage/session launch paths, scan for:
- **Duplicated policy checks** spread across planner/launcher/execution (claims, provider availability, labels, gating).
- **Split responsibility** where correctness relies on multiple call sites remembering to call a check.
- **Label lifecycle consistency**: add/remove paths should be symmetric and account for all session types.
- **New entrypoints** that bypass existing gates or helpers.

If any of the above appear, recommend centralizing the policy into a shared helper/module and reusing it.

## Review Decision Policy (Strict)

Use a hardline merge bar. Do not soften medium-or-higher concerns into an approval.

- **Only nits may remain unaddressed at merge time.**
- If a comment identifies correctness, reliability, safety, architecture, contract, test coverage, observability, or maintainability risk, it is **not a nit**.
- Concerns marked "verify", "worth checking", or "might be an issue" are not informational notes. They require confirmation before approval.
- If uncertain whether something is a nit, treat it as non-nit until proven otherwise.

### Allowed Outcomes

- **Approve**: all non-nit concerns are resolved in-code, or conclusively disproven with evidence.
- **Request changes**: any non-nit concern remains unresolved, unverified, or deferred.

### Not Allowed

- "Approve with comments" when comments include unresolved non-nits.
- Approving while asking for follow-up on medium/high-risk items.
- Downgrading meaningful concerns to "minor" to avoid blocking.

### Nits (Examples)

- Wording tweaks in comments/docs that do not affect behavior.
- Optional formatting/style preferences with no readability or maintainability impact.
- Non-substantive naming preferences where current naming is clear and consistent.

### Non-Nits (Always Blocking Until Resolved)

- Control-flow changes that could alter behavior.
- Potential runtime exceptions or missing fields/attributes.
- Data/contract/schema mismatches or payload bloat risks.
- Missing or weak tests for changed behavior.
- Architectural drift from ports/adapters, DI, or lifecycle boundaries.

## Review Pipeline

```
Work Agent creates PR
       │
[Stage 1: Code Review] (per-PR, immediate)
  Label: needs-code-review → code-reviewed OR needs-rework
       │
   ┌───┴───┐
Approved   Changes Requested
   │            │
   │       [Rework Loop] (up to max_rework_cycles)
   │            │
   │       Back to Code Review
   │
[Stage 2: Triage Review] (batch, threshold-triggered)
  Label: code-reviewed → triage-reviewed
       │
   Manual merge


Session FAILED/BLOCKED/TIMEOUT
       │
[Failure Investigation] (if triage_review_on_failure: true)
  - Triage agent reviews what went wrong
  - Uses _queue_triage_failure_review()
  - Helps identify patterns in failures
```

## Key Runtime Touchpoints

| Method | Purpose |
|--------|---------|
| `launch_review_session()` | Launch review agent |
| `scan_needs_rework_prs()` | Find PRs needing rework |
| `launch_rework_session()` | Re-launch work agent |
| `_queue_triage_failure_review()` | Queue triage to investigate failures |
| `control/github_workflow.py` | Review/rework discovery and queueing logic |
| `control/session_launcher.py` | Review and rework session launch paths |
| `infra/orchestrator.py` | Runtime facade that delegates to the review workflow helpers |

## Configuration

```yaml
review:
  code_review_agent: "agent:reviewer"
  code_review_label: "needs-code-review"
  max_rework_cycles: 2

  # Triage
  triage_review_agent: "agent:triage"
  triage_review_threshold: 5        # Batch review after N PRs
  triage_review_on_failure: true    # Investigate failures (default: true)
```
