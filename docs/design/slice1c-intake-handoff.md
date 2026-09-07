# #6914 slice 1c intake handoff

This implements accepted design §11 slice 1c on `a10bda5` in the existing
`issue-orchestrator-wt-champion-techlead-intake` worktree. It does **not** complete
#6914. No production historical data was imported or changed.

## Owners and authority

`SqliteIssueRunLedger` owns allocation, capability lifetime, immutable submission
and validator-attestation tables, processing acknowledgements, and repair. Its
private `CompletionIntakeTables` implementation uses the same SQLite transaction
boundary and exact allocated run identity. The added tables are
`completion_intake_runs`, `completion_intake_entries`,
`completion_validation_attestations`, and `completion_intake_processing`.
Disposition tables and the independently reviewed full store were not changed.

`CompletionIntakeArtifacts` stores raw/normalized bytes and validation outputs
outside agent worktrees. Envelopes and blobs are fsynced and atomically renamed
before registration. Repair reconciles orphan envelopes and verifies persisted
scalar columns, hashes and byte counts for authoritative external artifacts,
including entries already acknowledged as processed. Reading a completion also
verifies its certified run-contained validation copy against that authority. Invalid and corrected
submissions remain distinct immutable entries. Interrupted staging files are
retained; this slice introduces no evidence deletion API.

`CompletionEvidenceIntakeService` is the shared control owner for receipt
submission, validation queue draining, authenticated resume, exchange receipt
selection and terminal closure. Bootstrap injects the same instance into the
HTTP facade, processor, action/lifecycle owners and persistent exchange runner.
The durable entry is the queue item; an in-memory wakeup is not authority.
Startup/tick pumping resumes accepted but unprocessed receipts.

`ConfiguredCompletionEvidenceValidator` freshly invokes `ValidationRunner.run`
in an isolated checkout of the actual full commit, verifies workspace HEAD and
cleanliness before and after execution, then binds output to the exact receipt,
run, raw/normalized hashes and configured validator digest. Caller-supplied
validation JSON, session IDs, manifests and paths grant no authority. Failed
attestations remain readable and route through the existing validation retry
owner; publication readiness rejects them.

Normal coding/rework/Tech Lead terminal processing uses receipts. Persistent
coder advancement requires a new receipt for the current attempt and a passed
trusted attestation. A cached coder process is rebound by respawn when the
allocated run changes. Normal, no-completion and exception exits drain intake
before releasing a pair. Custody failures abort release. Terminal closure and
`resume_receipt` execution share the owner lock, so closure cannot overtake an
already-authorized synchronous resume operation.

## HTTP and producer contracts

All three routes use canonical OpenAPI models; public JSON schemas, generated
Python models and TypeScript declarations move with the handlers.

| Route | Request and authority | Result |
| --- | --- | --- |
| `POST /api/completion/submissions` | Normal callback/operator authentication **and** `X-Completion-Capability`; base64 `raw_bytes`, `content_sha256`, stable `submission_key`; no run/path selector | Durable `entry_id` and `content_sha256` receipt; malformed completion JSON can still receive a rejected-entry receipt |
| `POST /api/issues/{issue_number}/resume` | Same authentication plus an **open** capability bound to this issue and exact receipt; generated receipt body | Typed processing outcome; authorization and execution stay inside the intake owner's drain lifetime |
| `POST /api/validated-work/intake` | Operator authentication only; exact repository, positive issue, branch, full HEAD, normalized absolute candidate path, SHA-256, actor and reason | Discriminated `parked`, `refused`, or `validation_failed` result |

`coding-done` writes its raw candidate, submits it, and reports successful
submission only after the receipt. Raw SHA-256 is its deterministic retry key:
lost acknowledgements converge on one receipt; corrected bytes get a new key.
Changed bytes under an existing key are refused. An identical POST retry may
retrieve the old receipt after closure, but cannot reopen `/resume` authority.
Unavailable delivery retains the candidate and does not write a success marker.
The shared transport refuses redirects; capability values stay out of shell
command text and logs. A mode-0600 capability file outside the worktree supplies
the launch environment. Pre-capability allocated runs are not retro-authorized.

## Engine prerequisites and operator behavior

A running engine now requires a reachable Control API for completion delivery.
CLI modes that previously declared no API reject that mode before startup;
use `--api-port 0` for an allocated local port, or an explicit port. Provider
sandbox policy must permit the configured local endpoint. No provider or
production configuration was changed in this worktree.

Production `build_orchestrator` also rejects an absent `validation.quick.cmd`
before its first startup effect, **including when
`review.exchange.loop.require_validation` is false**. The intake validator is a
mandatory authority boundary separate from that legacy review-loop switch.
`Config.validate()` can still accept configurations for read-only commands without
quick validation; passing it alone does not imply engine startup readiness.
The explicit testing composition remains available for substituted dependencies.
The configured command and its toolchain must work in the isolated checkout;
there is no fabricated passing result or fallback to an agent sidecar.

Historical import verifies the selected repository/branch/full commit through
`WorkingCopy`, captures the candidate bytes (including invalid or hash-replaced
bytes), allocates a fresh recovery run and independent checkout, and runs fresh
configured validation. Successful import retains a local exact-head pin and
bounded historical artifacts, then admits **PARKED** evidence. Actor/reason are
audit fields, not authorization. Failed validation retains its owned result and
cannot admit work. An interrupted parked admission resumes from the receipt and
existing trusted attestation without replacing either. Publication still requires
a separate snapshot-bound recovery approval in a later slice.

## Independently reviewed store interface assumptions

The new admission-only `SqliteValidatedWorkIntakeStore` composes the existing
`DispositionDatabase.transaction`, `EvidenceAdmissionWriter.admit`,
`LineageClassifier`, and disposition row reader. It consumes typed
`EvidenceAdmission` / `AdmissionOutcome`, `ValidatedWorkAncestry`, and
`ValidatedWorkArtifactVerifier` boundaries. These existing interfaces are assumed
to retain their current transaction and identity semantics during store review.
It does not construct the full store, acquire publication claims, supply fake
liveness, or fabricate caller proofs. Artifact and ancestry checks use actual
local Git and content hashes.

The narrow adapter requires PARKED both on input and the resulting disposition
inside one transaction. Existing publishing/recovered records cannot be retargeted
by import. Changes to the independent store API should be reconciled at this
adapter/composition seam, not by weakening these checks.

## Verification checkpoint

- Current 22-module unit/contract/owner/local-Git suite: **923 passed**,
  including the previously green **872** (and original **517**) cases, the
  existing validation module and six new socket-free custody regressions.
  Focused validation/custody suite: **78 passed**. Both have one existing
  Starlette TestClient deprecation warning.
  Logs: `/private/tmp/intake-validation-checkpoint-tests.log` and
  `/private/tmp/intake-validation-custody-after.log`.
- Changed-source standard and strict Pyright: **0 errors, 0 warnings** each.
  Ruff checks on all changed Python files: **passed**.
- Actual import-linter: **10 contracts kept, 0 broken**. AST architecture
  guardrails: **passed** (exit 0); `git diff --check` is clean.
  Changed deterministic subprocess fixtures compile successfully.
- Quality guardrails run against the **unchanged original HEAD baseline**:
  **no slice-owned violations**. The whole comparison still exits **2 solely
  for unchanged prerequisite `infra/validated_work_attempts.py` (8 branch sites)**.
  Its reviewed replacement is root's integration responsibility. No baseline,
  suppression, rule configuration or metric-name changes are part of this slice.
- Root's deterministic exchange integration run returned **7 failed, 5 passed,
  1 deselected** before the repair described below. That result is not green.
  The corrected snapshot still requires root's complete rerun:
  `.venv/bin/pytest -q tests/integration/test_persistent_review_exchange_integration.py -m 'not live_codex'`.
  A sandbox rerun stopped at the first fixture's localhost `socket.bind` with
  `PermissionError` before any test/provider process ran; log:
  `/private/tmp/intake-validation-nonlive-sandbox.log`.
- Exactly one retained internal reviewer: `/root/intake_internal_reviewer`
  (Gauss), **gpt-6-astra**, following `repo-specific/prompts/internal-review.md`.
  Its earlier approval was superseded by root's required refactoring. The same
  reviewer approved the codec/mirror refactor, then was retained again for the
  validation-output repair and final abstraction pass. The final conversational
  verdict accompanies this checkpoint; no new reviewer was spawned.
- No broad gate, live-provider test, remote write, push, PR, merge, completion
  command, production restart, commit, or index-authority bypass was performed.

Focused command:

```sh
.venv/bin/pytest -q \
  tests/unit/test_completion_evidence_intake.py \
  tests/unit/test_completion_intake_routes.py \
  tests/unit/test_historical_completion_intake.py \
  tests/unit/test_issue_run_evidence.py \
  tests/unit/test_completion_processor.py \
  tests/unit/test_coding_done_tech_lead_artifacts.py \
  tests/unit/test_orchestrator_resume.py \
  tests/unit/entrypoints/test_cli_run_modes.py \
  tests/unit/execution/test_persistent_session_exchange.py \
  tests/unit/execution/test_persistent_review_exchange_runner.py \
  tests/unit/test_control_api_issue_actions.py \
  tests/unit/test_control_api_auth.py \
  tests/unit/test_session_controller.py \
  tests/unit/test_review_exchange_lifecycle.py \
  tests/unit/test_public_contract_schemas.py \
  tests/unit/test_ui_openapi_generated.py \
  tests/unit/test_ui_openapi_payloads.py \
  tests/unit/test_git_working_copy.py \
  tests/unit/test_orchestrator.py \
  tests/unit/test_action_applier.py \
  tests/unit/test_validation.py \
  tests/integration/test_completion_validation_custody.py --tb=short
```

## Refactoring and original-baseline quality

Root rejected the proposed baseline increases. `quality/guardrails-baseline.json`
is byte-identical to HEAD. Guardrails now report no new slice-owned failure;
no new owner was split merely to distribute counts across smaller files.

The six oversized files now delegate cohesive responsibilities:

| File | Responsibility extracted | HEAD lines | Current lines |
| --- | --- | ---: | ---: |
| `control/action_applier.py` | Review feedback retrieval/rendering into `review_feedback.py` | 1802 | 1779 |
| `control/completion_processor.py` | Validation artifact projection/copy into `CompletionValidationArtifacts` | 2707 | 2698 |
| `entrypoints/bootstrap.py` | Publish-recovery composition into existing `bootstrap_completion.py` | 1311 | 1308 |
| `execution/git_working_copy.py` | Revision identity and historical selection into `GitRevisionReader` | 843 | 838 |
| `execution/persistent_session_exchange.py` | Pair validation projection into `PairValidationMirror` | 3225 | 3091 |
| `infra/orchestrator.py` | Runtime shutdown/drain phases into existing lifecycle/supervisor owners | 1136 | 1135 |

Pure domain policies own receipt eligibility/publication requirements, exact
owned-validator result binding, historical eligibility and evidence identity.
`SubmissionEnvelope` and `AttestationEnvelope` own bidirectional persistence codecs
in execution: envelope decoding, SQL column layout and observed row/custody
consistency checks stay together. The ledger supplies actual observations, not
caller-assembled expected proof tuples. It retains transactions, bytes and repair.
`PairValidationMirror.completion_error` owns mandatory receipt refresh and optional
HEAD-freshness inspection; there is no Boolean decision carrier in domain.
Control retains I/O ordering, processing acknowledgements and the closure lock.
No caller-supplied proof or liveness flag was introduced at a port.

The four original policy-count owners now measure **2 / 2 / 1 / 0** respectively
for completion intake, historical intake, intake ledger, and historical custody.
The extracted pair-validation owner and persistence codec each measure 2. The complete guardrail comparison,
including semantic and typed-boundary rules, reports only the independent store
prerequisite mentioned above. Import-linter reports **10 contracts kept, 0 broken**;
AST architecture guardrails and repository instruction/documentation checks pass.

## Deterministic integration repair

Root's log `/tmp/techlead-intake-refactor-nonlive-integration.log` exposed a
production composition defect, not an agent/test timing problem. Each of the
seven failures follows the receipt owner into
`ConfiguredCompletionEvidenceValidator.validate`; the post-command cleanliness
check raises, and protocol-retry/terminal drain attempts re-execute the still
pending receipt and reach the same refusal. The configured command was `true`.

`ValidationRunner.run` always calls `ValidationRecordStore.write`. Intake had
constructed that store with the isolated checkout as its default cache root.
Besides the externally staged stdout/stderr, the runner therefore produced:

`receipt-owner/completion-validation-workspaces/<entry-id>-<uuid>/.issue-orchestrator/validation/<HEAD>.json`

The isolated clone has no agent-local Git exclusion. Git correctly reported
that owner-produced file as untracked. Root's original fixture directories had
expired before inspection; the socket-free real-owner reproduction created the
same failure twice (successful and failed shell commands). Exact observed files
in that reproduction were:

- `.issue-orchestrator/validation/95f7bdc02ebca511cb540dc980361e6eb9a6ab16.json`
- `.issue-orchestrator/validation/532b1cf3e1cde27367a777d90925140e87f41902.json`

Before repair, those two positive cases failed and four mutation-refusal cases
passed (`/private/tmp/intake-validation-custody-before.log`). The earlier unit
fixtures substituted `WorkingCopy.has_uncommitted_changes=False`, masking this
filesystem interaction; the new regression module uses real local Git, isolated
clones, the command runner and SQLite receipt/attestation owners.

The complete `ValidationRecordStore` persistence responsibility now lives in
`control/validation_record_store.py`, re-exported from `control.validation` for
existing callers. An explicit `record_directory` separates record storage from
`worktree`, which still controls command cwd and runtime tool environment. The
legacy default cache behavior remains unchanged. `control/validation.py` shrank
from 864 to 777 lines, with no quality-baseline adjustment.

Intake creates the runner inside its existing external temporary output scope.
The cache is written under `<external-staging>/records/<HEAD>.json`; stdout and
stderr remain in that staging scope. The returned bytes then enter the existing
immutable `<entry-id>-validation` envelope and certified run-copy flow. No
repository ignore rule, dirty-file exception, cleanup-before-check, cached-result
authority, or validation suppression was introduced. Both pre/post HEAD and
cleanliness checks remain intact.

The six regressions prove passed and failed commands produce durable attested
logs and a clean isolated checkout across ledger restart. Tracked edits,
untracked output, forged files under `.issue-orchestrator/validation`, and an
otherwise clean changed HEAD still refuse attestation and leave the receipt
pending. The full HTTP/subprocess integration result remains unverified until
root runs the command above outside this sandbox.

The repair changes four source/test paths relative to the approved 91-file
snapshot, plus this handoff: `control/completion_intake_validation.py`,
`control/validation.py`, `control/validation_record_store.py`, and
`tests/integration/test_completion_validation_custody.py`. Other reviewed files
retain their previous hashes. Store/escrow prerequisite integration remains
parent-owned.

## Remaining invariants and root sequencing

“Processed” acknowledges intake validation/historical admission, not exactly-once
publication. General disposition capture across all validated heads, general
escrow and retention/release policy, lifecycle preservation, exact-head publishing,
and Control Center recovery UI remain separate accepted-design slices. The legacy
operator manual publication recovery path is not migrated by this slice.
No implementation claim here permits publication from arbitrary historical JSON.

Root owns named-file commits if needed, hook/index authority, full gates scheduled
one at a time, the deterministic subprocess rerun, and independent external review
after internal approval. Do not mark #6914 complete from this checkpoint.

Implemented abstraction findings: receipt execution and closure now have one
owner lifetime; persistent attempts use a typed intake boundary; shared processing
outcomes live in domain; receipt-owned artifacts bypass legacy deletion; historical
admission composes existing transaction/lineage owners without fake liveness.
The refactoring additionally separates pure eligibility/identity/integrity decisions
from I/O, validation projection from completion coordination, and shutdown draining
from the orchestrator facade.
No abstraction fix was deferred. The final abstraction pass includes the cohesive execution codec and mirror
fixes requested by the retained reviewer. No storage schema decisions remain
in the pure domain correspondence rule.
There are no visual UI/control changes in this slice.

## Exact changed-file manifest

The corrected checkpoint contains **94 files** listed below. After the
final reviewer verdict, SHA-256 hashes for every listed file (including this
handoff) are written to `/private/tmp/techlead-intake-validation-repair-files.sha256`.
The external manifest avoids a self-referential hash; it is the named-file
commit/review input. The unchanged quality baseline is excluded.

- `contracts/public/completion.receipt.json`
- `contracts/public/completion.resume.json`
- `contracts/public/completion.submission.json`
- `contracts/public/historical_intake.command.json`
- `contracts/public/historical_intake.outcome.json`
- `contracts/public/historical_intake.parked.json`
- `contracts/public/historical_intake.refused.json`
- `contracts/public/historical_intake.validation_failed.json`
- `docs/api/ui-openapi.json`
- `docs/design/slice1c-intake-handoff.md`
- `src/issue_orchestrator/contracts/public.py`
- `src/issue_orchestrator/contracts/ui_openapi_models.py`
- `src/issue_orchestrator/control/action_applier.py`
- `src/issue_orchestrator/control/background_job_supervisor.py`
- `src/issue_orchestrator/control/completion_exchange_intake.py`
- `src/issue_orchestrator/control/completion_intake.py`
- `src/issue_orchestrator/control/completion_intake_validation.py`
- `src/issue_orchestrator/control/completion_processor.py`
- `src/issue_orchestrator/control/completion_result_artifacts.py`
- `src/issue_orchestrator/control/completion_review_exchange.py`
- `src/issue_orchestrator/control/completion_types.py`
- `src/issue_orchestrator/control/completion_validation_artifacts.py`
- `src/issue_orchestrator/control/historical_completion_intake.py`
- `src/issue_orchestrator/control/issue_run_allocator.py`
- `src/issue_orchestrator/control/orchestrator_deps.py`
- `src/issue_orchestrator/control/review_exchange_lifecycle.py`
- `src/issue_orchestrator/control/review_feedback.py`
- `src/issue_orchestrator/control/session_controller.py`
- `src/issue_orchestrator/control/session_env.py`
- `src/issue_orchestrator/control/session_launcher.py`
- `src/issue_orchestrator/control/validation.py`
- `src/issue_orchestrator/control/validation_record_store.py`
- `src/issue_orchestrator/domain/completion_custody_integrity.py`
- `src/issue_orchestrator/domain/completion_intake.py`
- `src/issue_orchestrator/domain/completion_intake_policy.py`
- `src/issue_orchestrator/domain/completion_processing.py`
- `src/issue_orchestrator/domain/historical_intake.py`
- `src/issue_orchestrator/domain/historical_intake_policy.py`
- `src/issue_orchestrator/entrypoints/_auth_middleware.py`
- `src/issue_orchestrator/entrypoints/bootstrap.py`
- `src/issue_orchestrator/entrypoints/bootstrap_completion.py`
- `src/issue_orchestrator/entrypoints/bootstrap_run_services.py`
- `src/issue_orchestrator/entrypoints/cli_run_modes.py`
- `src/issue_orchestrator/entrypoints/cli_tools/coding_done.py`
- `src/issue_orchestrator/entrypoints/cli_tools/completion_submit.py`
- `src/issue_orchestrator/entrypoints/cli_tools/orchestrator_resume.py`
- `src/issue_orchestrator/entrypoints/cli_tools/reviewer_done.py`
- `src/issue_orchestrator/entrypoints/completion_intake_routes.py`
- `src/issue_orchestrator/entrypoints/control_api.py`
- `src/issue_orchestrator/entrypoints/control_api_issue_routes.py`
- `src/issue_orchestrator/entrypoints/web_retry_history_routes.py`
- `src/issue_orchestrator/execution/completion_intake_artifacts.py`
- `src/issue_orchestrator/execution/completion_intake_codec.py`
- `src/issue_orchestrator/execution/completion_intake_ledger.py`
- `src/issue_orchestrator/execution/git_revision_reader.py`
- `src/issue_orchestrator/execution/git_tools.py`
- `src/issue_orchestrator/execution/git_working_copy.py`
- `src/issue_orchestrator/execution/historical_intake_custody.py`
- `src/issue_orchestrator/execution/intake_disposition_verification.py`
- `src/issue_orchestrator/execution/issue_run_ledger.py`
- `src/issue_orchestrator/execution/persistent_pair_validation.py`
- `src/issue_orchestrator/execution/persistent_review_exchange_runner.py`
- `src/issue_orchestrator/execution/persistent_role_prompt_policy.py`
- `src/issue_orchestrator/execution/persistent_session_exchange.py`
- `src/issue_orchestrator/infra/orchestrator.py`
- `src/issue_orchestrator/infra/validated_work_intake_store.py`
- `src/issue_orchestrator/ports/completion_intake.py`
- `src/issue_orchestrator/ports/historical_intake.py`
- `src/issue_orchestrator/ports/issue_run_allocator.py`
- `src/issue_orchestrator/ports/issue_run_evidence.py`
- `src/issue_orchestrator/ports/review_exchange_runner.py`
- `src/issue_orchestrator/ports/working_copy.py`
- `src/issue_orchestrator/static/js/ui-contracts.d.ts`
- `tests/conftest.py`
- `tests/fixtures/completion_receipt.py`
- `tests/fixtures/interactive_review_agent.py`
- `tests/fixtures/synthetic_review_exchange_tui.py`
- `tests/integration/completion_intake_fixture.py`
- `tests/integration/test_completion_validation_custody.py`
- `tests/integration/test_persistent_review_exchange_integration.py`
- `tests/run_allocation_helpers.py`
- `tests/unit/entrypoints/test_cli_run_modes.py`
- `tests/unit/execution/test_persistent_review_exchange_runner.py`
- `tests/unit/execution/test_persistent_session_exchange.py`
- `tests/unit/test_action_applier.py`
- `tests/unit/test_coding_done_tech_lead_artifacts.py`
- `tests/unit/test_completion_evidence_intake.py`
- `tests/unit/test_completion_intake_routes.py`
- `tests/unit/test_completion_processor.py`
- `tests/unit/test_control_api_issue_actions.py`
- `tests/unit/test_historical_completion_intake.py`
- `tests/unit/test_orchestrator_resume.py`
- `tests/unit/test_review_exchange_lifecycle.py`
- `tests/unit/test_session_controller.py`
