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
- [docs/ai/REVIEW_WORKFLOW.md](docs/ai/REVIEW_WORKFLOW.md) - Full workflow documentation

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

## Key Methods (orchestrator.py)

| Method | Purpose |
|--------|---------|
| `queue_code_review()` | Queue PR for review |
| `launch_review_session()` | Launch review agent |
| `process_pending_reviews()` | Process queue each loop |
| `scan_needs_rework_prs()` | Find PRs needing rework |
| `launch_rework_session()` | Re-launch work agent |
| `check_triage_review_trigger()` | Check batch threshold |
| `_queue_triage_failure_review()` | Queue triage to investigate failures |
| `process_pending_triage_reviews()` | Process triage queue |

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
