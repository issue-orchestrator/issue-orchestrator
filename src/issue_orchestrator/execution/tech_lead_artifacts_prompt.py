"""The tech-lead decision-artifact contract, as the prompt states it (ADR-0031).

Moved out of :mod:`.setup_wizard_prompts`, which is at its size budget; it is
one self-contained section (what to write, the bounds, which targets each
action kind may name) that the generated prompt interpolates whole.
"""

from .tech_lead_triage_prompt import TECH_LEAD_DECISION_TRIAGE_RULES

# Shared artifact-contract text for the tech_lead prompt (plain string, NOT an
# f-string: the JSON example's braces must survive interpolation below).
TECH_LEAD_ARTIFACTS_SECTION = (
    """## Required Output Artifacts (MANDATORY)

Before running `coding-done`, write BOTH files into your tech-lead-data
directory (next to the manifest; the directory exists even when there is
no PR manifest):

- `tech-lead-report.md` - your human-readable tech-lead report. It MUST
  mention every finding id and action id from the decision file.
- `tech-lead-decision.json` - the machine-readable decision the orchestrator
  validates and acts on.

Compact `tech-lead-decision.json` example:

```json
{
  "schema_version": 1,
  "summary": "One infra pattern found across the batch.",
  "findings": [
    {
      "id": "T1",
      "title": "CI runner disconnects mid-build",
      "classification": "infra",
      "evidence": ["pr-123-diff.txt", "orchestrator log lines 1020-1041"]
    }
  ],
  "proposed_actions": [
    {
      "id": "A1",
      "action_type": "post_comment",
      "target_number": 123,
      "target_is_pr": true,
      "body": "Diagnosis: CI runner disconnects mid-build (see T1).",
      "finding_ids": ["T1"]
    },
    {
      "id": "A2",
      "action_type": "create_issue",
      "title": "Stabilize CI runner disconnects",
      "body": "Three PRs in this batch hit the same disconnect (T1).",
      "labels": ["bug"],
      "area": "ci-runtime",
      "finding_ids": ["T1"]
    }
  ]
}
```

- Finding `classification` is one of: `infra`, `task`, `agent`, `systemic`.
- Ids are canonical: findings are `T<n>` (`T1`, `T2`, ...) and actions are
  `A<n>` (`A1`, `A2`, ...), no leading zeros, unique across both lists. The
  report must mention every id as an exact token (`T10` does not cover `T1`).
- Every finding MUST include `evidence`: at least one non-empty string
  reference into the inputs you were given (file names, log line ranges).
- Keep machine fields within their hard bounds: finding and proposed-action
  `title` values are at most **300 characters**; the decision `summary` is at
  most 5,000 characters; each proposed-action `body` is at most 20,000
  characters; and each finding has at most 20 evidence references. Each
  proposed action has at most 10 labels, and each individual label is at most
  100 characters. `pattern_signature` is at most 200 characters; `area` is at
  most 50 characters. Put the concise diagnosis in `title` and the full
  explanation in `evidence`, the report, or an action body. The decision may
  contain at most 50 findings and 20 proposed actions.
- `create_issue` labels must be plain descriptive labels. Workflow labels
  are rejected as a contract violation: anything like `in-progress`,
  `needs-*`, `*-reviewed`, `*-failed`, `publish-*`, `blocked*`, `agent:*`,
  or `tech_lead:*` corrupts orchestrator label truth (matching is
  case-insensitive).
- Targets are scoped to what you were launched to audit, and the scope
  splits by action kind:
  - `post_comment` and `escalate_to_human` may only target the manifest PRs or
    your own tracking issue (batch review), `focus_issue_number` (failure
    investigation), or THIS tracking issue and the blocked items in
    `tech-lead-data/blocked-item-triage.json` (health review).
  - `defer_to_tracker` may only target `focus_issue_number` in a failure
    investigation, with `tracker_number` from `recovery_tracker_numbers` in
    `tech-lead-data/recovery-context.json`. It is not a batch or health action.
  - Act-level `reset_retry`, `kill_hung_session`, `recover_validated_work`, `release_withheld_review`, `propose_decision`, and `resolve_block` may only target the
    `focus_issue_number` (failure investigation), or an issue number listed
    in the snapshot's `problem_cohort` or in `blocked-item-triage.json` (health review). A batch review owns
    no act-level target at all for these reset/stop operations: manifest entries are PRs and the anchor is
    bookkeeping, so resetting either would hit the wrong entity.
  - Act-level `request_rework` targets only a PR in your orchestrator-supplied
    `scoped-rework-targets.json`, with `target_is_pr: true`, `finding_ids` and
    requested feedback in `body`. Its recorded head and labels bind approval.
    Batch targets come from the reviewed manifest; health/failure targets come
    from the supplied validated problem facts. Never invent a PR target.
  Any other target is rejected at completion. `create_issue` and
  `flag_pattern` carry no target.
- `flag_pattern` requires a stable `pattern_signature` (a short reusable slug
  naming the recurring pattern). Both `flag_pattern` and
  root-cause/design-review `create_issue` actions may carry an `area` naming
  their component or seam. The orchestrator keeps a durable case file issue
  per signature: the first observation opens it, and later observations of
  the SAME signature accrue there as evidence. Its `body` must explain the
  causal mechanism and the suggested fix; that diagnosis is copied into any
  routed promotion so the target issue is actionable without hidden context.
- Classify every `flag_pattern` with `fix_class`: `"code"` when a code change
  in some repository fixes it, `"human"` when it needs a human decision,
  credential, or configuration change. This is the promotion gate. Once a
  `fix:code` signature accrues enough observations, the orchestrator files it
  as a runnable issue in the repository its `area` routes to, so pick the
  `area` that names WHERE the fix belongs. `fix:human` findings are never made
  runnable — a human-gated problem turned into agent work only manufactures
  doomed rework. Omit `fix_class` when you genuinely cannot tell; an
  unclassified signature keeps accruing evidence and is never promoted. Only
  `flag_pattern` may carry `fix_class`.
- Step back on recurrence: multiple case files on one area/seam, or shipped
  fixes followed by recurrence there, are a mandate to fix the design—not to
  keep applying point patches. Propose a root-cause design review issue via
  `create_issue`; name the seam, carry the same `area`, cite the case files and
  accumulated shipped-fix/patch evidence, and recommend deep rework.
- Do not file a duplicate. Before proposing a `create_issue`, check the open
  issues you were given. If your follow-up already exists as an open issue, set
  `duplicate_of` to that issue number — this is your (untrusted) dedup intent.
  The orchestrator verifies it against trusted facts when available: a verified,
  in-scope duplicate receives your observation directly; otherwise the
  observation accrues to the durable case file for that recurring class, with
  the cited candidate preserved for a human to reconcile. That accrual writes an
  orchestrator-owned ledger, so it depends on this deployment's `flag_pattern`
  authority: under `flag_pattern: execute` the observation accrues, and under
  `flag_pattern: propose` there is no durable ledger to accrue to, so it becomes
  a gated new issue naming the candidate instead. A re-sighting of a standing
  problem therefore keeps ONE durable ledger rather than adding an open issue
  per review. Add a `pattern_signature` naming the recurring class when
  you want the sighting to join an existing case file (a lexical near-duplicate
  you did NOT cite is still gated as a fresh issue). Always still provide
  `title` and `body`. `duplicate_of` is only valid on `create_issue`.
- `defer_to_tracker` requires a `tracker_number` naming a real OPEN issue
  other than the target, admitted by the immutable launch grant. Put the next
  remedy and why that prerequisite owns it in `body`. Only `defer_to_tracker`
  may carry `tracker_number`. Its explanation and durable binding commit as one
  completion-mandatory command; failed publication/storage fails completion.
  See the Failure Investigation Flow for the finite deadline and release rules.
- Valid `action_type` values: `post_comment`, `create_issue`,
  `escalate_to_human`, `defer_to_tracker`, `flag_pattern`, `reset_retry`, `kill_hung_session`, `request_rework`, `recover_validated_work`, `release_withheld_review`, `propose_decision`, `resolve_block`.
- For retained validated work, propose `recover_validated_work` only for an issue listed in `tech-lead-data/validated-work-recovery-targets.json`. The orchestrator binds the full listed authority snapshot, and any later evidence, PR, or remote-baseline change makes approval stale.
- When an issue's only fault is its own `blocked-failed` while its open, CI-green PR waits on code review (review discovery skips it as `issue_blocked`), propose `release_withheld_review` with the ISSUE as `target_number`: it puts `pr-pending` on, then removes only `blocked-failed`, so the PR's review runs. Never `reset_retry` such a PR. The orchestrator re-verifies at apply time that exactly one open PR is the issue's, its checks are green, no session or claim holds the issue, review validity withholds the review for that block alone, and no failure was recorded after you observed it; otherwise it refuses the release with a typed reason. It is not destructive, so the charter lets it run on its own when the flow role executes.
- For an existing PR needing scoped corrections, propose `request_rework` with
  `target_is_pr: true`, its PR `target_number`, `finding_ids`, and actionable
  feedback in `body`. Target only PRs in `scoped-rework-targets.json`; those
  immutable launch facts bind the repository, linked issue, head and branch.
  The report is preserved as the coder instruction. Default authority is
  `propose`: approve in the rework proposal panel or remove the existing
  proposed-tech-lead gate. Execution preserves the branch and uses normal
  rework policy; changed heads require fresh review, and merged PRs receive
  one forward fix. `flag_pattern` promotion is a different lane.
"""
    + TECH_LEAD_DECISION_TRIAGE_RULES
    + """- Proposals are intent, not execution: the orchestrator decides what to
  execute per its configured authority. Act-level proposals (`reset_retry`,
  `kill_hung_session`, `request_rework`, `recover_validated_work`, `release_withheld_review`, `propose_decision`, `resolve_block`) under `propose` authority become reviewable GitHub
  issues carrying the `proposed-tech-lead` label; a human approves one by
  removing that label, and the orchestrator re-checks the target's state
  before executing — stale proposals are closed with a comment, not
  executed. `reset_retry` is destructive and ALWAYS waits for that approval;
  `kill_hung_session` under `tech_lead.authority.kill_hung_session: execute`
  runs directly with its execution-time re-check. Never propose or
  touch the `proposed-tech-lead` label yourself; it is orchestrator-owned and
  rejected like other workflow labels.
- A completed session missing either artifact — or violating any rule
  above — is recorded as FAILED and marked tech-lead-failed.
"""
)
