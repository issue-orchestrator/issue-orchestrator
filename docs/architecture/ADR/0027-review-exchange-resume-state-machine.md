# ADR-0027: Review-exchange resume decision is one named state machine

## Status

Accepted.

## Context

The persistent-session review-exchange runner produces `summary.json`
artifacts that the next orchestrator tick consults to decide what to
do: reuse a cached approval, halt on a deterministic terminal
outcome, retry under the no-completion budget, or spawn a fresh
exchange. Pre-this-ADR that decision was inferred at three call
sites:

1. **Summary writer** — chose which fields to omit (`head_sha` /
   `validation_passed`) based on outcome status, encoding policy via
   absence.
2. **Cache loader** — walked the filesystem to find a sibling
   validation-record.json, then translated its absence into "no
   cache" via a bare `None` return.
3. **No-completion counter** — encoded the `*_no_completion` reason
   classification inline in a substring check.

Each of those sites had its own implicit copy of the
`(status, reason, head_match, validation_state)` matrix. They drifted
against each other across PRs. The resulting bug class:

- **PR #6267** (loop bound). Initial fix to bound retries; counter
  classifies on `_no_completion` suffix.
- **PR #6268** (continuous mirror). Unrelated, but exposed the seam.
- **PR #6270**. Tried to embed `head_sha` to fix the OK-respawn bug
  (tixmeup #359 / #361). Two rounds of review feedback:
  - Round 1: embedded `head_sha` on error summaries made
    `*_no_completion` cache-hittable; bypassed the budget; halted
    on first failure.
  - Round 2: removing the embedding from all error summaries made
    `coder_protocol_error` un-cacheable too — but the counter only
    classifies `_no_completion`, so protocol errors fell through to
    "spawn fresh forever."
- Each round of review feedback fixed one cell of the matrix and
  broke another. The matrix was never made explicit.

## Decision

The cache/retry policy is owned by **one named module** in the
domain layer:
[`domain/review_exchange_resume.py`](../../../src/issue_orchestrator/domain/review_exchange_resume.py).

It exposes:

- `ResumeDecision` enum — six variants naming every action the cache
  loader can ask the caller to take: `REUSE_APPROVAL`, `REUSE_HALT`,
  `COUNT_NO_COMPLETION_AND_RETRY`, `IGNORE_STALE`, `NO_CACHE`,
  `INVALID_SUMMARY`.
- `ResumeFacts` dataclass — pure inputs to the decision (status,
  reason, cached/current head_sha, cached/current validation passed,
  require_validation flag).
- `decide(facts) -> ResumeDecision` — pure function, no I/O. The
  whole `(status, reason)` matrix lives here as one frozen-set
  lookup plus a precedence ladder.
- `is_no_completion_reason(reason) -> bool` — single classifier the
  no-completion counter consumes.

Three downstream consumers route through the same module:

1. **Summary writer** (`execution/persistent_session_exchange.py`)
   records facts unconditionally (`head_sha` and `validation_passed`
   when knowable). Policy is no longer encoded by selective omission.
2. **Cache loader** (`control/completion_review_exchange.py`) builds
   `ResumeFacts` from filesystem state + cached summary, calls
   `decide()`, and returns a `ResumeResolution(decision, outcome,
   cache_metadata)` to the caller. Bare `None` returns are gone —
   every "no cache" reason has its own named variant.
3. **No-completion counter** (`execution/session_output_adapter.py`)
   calls `is_no_completion_reason()` instead of pattern-matching the
   reason string inline.

### Adjacent decisions made in the same PR

- **`summary.json` is self-describing.** The writer always embeds
  `head_sha` and `validation_passed` when the validation record is
  readable. The cache loader prefers those embedded facts; legacy
  summaries fall back to walking the filesystem.
- **`parent_session_name` on the review-exchange manifest.** Review-
  exchange runs explicitly link to their parent coding session so
  the cache loader and counter scope candidates by data, not by
  walking `sessions/` in mtime order and inferring boundaries from
  run_dir name patterns.
- **Typed manifest sections.** The two places the runner writes the
  manifest go through frozen dataclasses
  (`ReviewExchangeManifestHeader`, `ReviewExchangeRecordingPaths`)
  with `to_manifest_fields` and `from_manifest` symmetric methods.
  Round-trip tests pin field-set symmetry. Adding a field forces
  every call site that builds the section.
- **State-table + production-layout joint tests.** One parametrized
  unit test exhausts the matrix at the `decide()` level. One
  production-layout joint test exercises the real
  `CompletionReviewExchange` against the real
  `FileSystemSessionOutput` over the actual disk shape production
  produces. Adding a new `(status, reason)` row is a one-line change
  in `decide()`, one row in the unit test, and one row in the joint
  test — three places, all in lockstep, all visible to a single
  `grep`.

## Consequences

### Good

- Every cell of the matrix is enumerable. `_KNOWN_STATUS_REASON_PAIRS`
  in the resume module is the source of truth; the parametrized
  state-table test fails closed when a new pair is added without
  updating the matrix.
- Adding a new outcome takes a single PR-shaped change: add the
  reason constant, add the row to `decide()`, add the row to the
  state-table test, add the row to the production-layout joint test.
  No drift across writer/loader/counter possible.
- The cache loader's bare `None` return is gone. Callers dispatch on
  named variants, and "no cache" reasons that mean different things
  (head moved vs. retryable timeout vs. corrupted state) are
  distinguishable at the call site.
- Round-trip-tested manifest sections catch schema drift between
  writer and reader at the unit-test level, before production hits
  it.

### Costs

- The matrix is enumerable but still has six variants and a
  precedence ladder. The decision logic isn't trivial — but it
  fits in one file, one function, one test class.
- Backwards-compat with legacy summaries (no `head_sha` /
  `validation_passed` / `parent_session_name`) means there's a
  fallback path for each. Legacy paths are explicitly tested but
  add code volume; they're a one-time tax we'll drop after summaries
  written under the new shape are universally rolled out.
- Manifest sections that aren't yet typed (`manifest_accessor.py`'s
  reads of artifact dicts, retention metadata, etc.) still use loose
  `dict.get`. Threading those through typed dataclasses is future
  work; this ADR establishes the pattern.

### Deferred

- **Turn packets.** The original review feedback also proposed making
  the agent contract narrower: orchestrator builds a complete
  `ReviewExchangeTurnPacket` for each role's round, agent reads one
  packet and writes one result, no artifact discovery from the
  agent's side. That's a separate refactor on the agent contract,
  meaningful but distinct from the cache-policy seam this ADR owns.
  The resume-decision boundary established here is the clean
  attachment point for that follow-up.

## Related

- ADR-0026 — issue-lifetime persistent exchange pair (the
  registry-owned coder/reviewer subprocess pair this exchange runs
  on top of).
- PRs in the immediate history:
  - #6267 (loop bound), #6268 (continuous mirror),
  - #6270 (head_sha embedding — closed; superseded by this PR).
