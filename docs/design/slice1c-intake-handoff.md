# #6914 slice 1c intake handoff

This implements accepted design §11 slice 1c, originally based on `a10bda5`, in
`issue-orchestrator-wt-champion-techlead-intake`. Root committed the reviewed
94-file implementation as `71f53e5106a87bcdd30f34a7cb872060f6de7003`, then merged
escrow `7381a4d` (including store A2 `7e43e20` and the prior policy fix) and
recovery ancestry `77cc282`. The combined compatibility base is committed
`2055cf3740897f6bf5f6ed221c5a22d91b2c5402`; root committed its compatibility
repairs as `629dff74bf90f6af75f7fca4d1ae1e811f106d08`, the base for the fixture
audit below. This does **not** complete #6914.
No production historical data was imported or changed.

## F1/F2 correctness repair against c37543d

Expected parent: `c37543d033ee406af31b6e99ab87988fac16e650`. Root's prior
real-listener scenario checkpoint passed all 59 scenarios/boundary tests. The
independent external report for that parent is **incomplete, not approved**;
these repairs address its established F1/F2/A1 findings but do not complete
that review or #6914.

F1: `domain/completion_custody.py` defines the shared 4 MiB artifact contract.
The writer validates every blob and the envelope before creating staging or
publishing an immutable directory. Oversized configured-validator stdout or
stderr produces a failed attestation. Complete diagnostic bytes remain in
ordered `stdout.log.part-NNNNNNNN` / `stderr.log.part-NNNNNNNN` files, each
bounded and hash-checked by ordinary repair. The corresponding log path holds
an explicit JSON failure descriptor containing ordered part names, sizes and
hashes, plus the complete stream size/hash. Concatenating those parts in order
reconstructs the exact original bytes. The attested validation JSON records
`custody_failure`; this is neither truncation nor a successful validation.
At/below the limit, the normal log bytes and behavior remain unchanged.

F2/A1: allocation records both `agent_label` and `completion_task` independently
of the session slot key. Production Tech Lead sessions may use CODE slots;
the allocation owner captures TECH_LEAD completion policy from the current
configured Tech Lead agent. The allocator requires the read-only typed
`IssueRunRoleConfiguration` port, supplied by the same live Config in bootstrap.
Previously allocated roles remain immutable across configuration changes.
`CompletionIntakeRuntime.processing_context(receipt, run)` returns one typed
`RegisteredCompletion` containing the owner-read record, artifact and
`CompletionRunRole(issue_number, task, agent_label)`. Read/artifact consumers
reuse this handoff. Direct and resumed receipt processing select policy from
it before effects, reject caller issue/role mismatch and missing/invalid role,
and reject Tech Lead configuration mismatch before applying the existing
launch-authority/decision-pair gate. A frozen `CompletionProcessingPolicy`
then carries the resolved role through pre-action gates, action shaping,
reviewer approval (including validation reroutes), and clean-audit exception
handling. Settings edits during an invocation cannot reselect that role.
The same retained reviewer reproduced this settings-change race; paired tests
pause at the external SessionOutput port and apply real settings updates to
prove missing authority still rejects, valid Tech Lead audits still suppress
comments, and an in-flight coder remains a coder. Agent JSON, filenames and
manifests do not select receipt authority. Legacy filename resolution remains confined to
non-receipt processing.

SQLite `issue_runs` gains nullable `agent_label TEXT` and `completion_task TEXT`
columns. Existing rows are not backfilled: unknown legacy roles fail closed at
receipt processing. `IssueRunRecord` exposes corresponding keyword-only
optional fields to represent that absence explicitly. Run-record encoding, decoding, storage comparison and column ordering live
in one execution codec; schema creation/upgrade has one SQLite schema owner.
INSERT uses named parameter bindings from that same codec. Preservation's independently
added branch and terminal binding fields are untouched; this repair adds no
terminal binding or terminal allocation fields. The preservation agent paths
were unavailable for message delivery; root must propagate the constructor,
record/codec and schema join explicitly. The repair does not import or modify
historical production data or any sibling worktree.

Verification during this repair: 347 focused tests passed, including exact
stdout/stderr size boundaries, complete overflow evidence, correction,
interrupted attestation/reopening, real local configured commands, receipt
resume and direct processing, missing/tampered/valid Tech Lead authority,
legacy migration and current-configuration allocation. Both pyright modes
passed. Actual `make lint-arch`, including the original quality baseline,
passed. Semgrep used its normal checks with sandbox-local log/settings paths
and the installed CA bundle; network version discovery and telemetry were
disabled. No quality suppression or baseline edit was made. Final retained
Gauss review and the exact hash manifest are reported in the checkpoint
message; older approvals below do not approve this new repair.

Implemented abstraction findings: A1's typed allocation-to-receipt role
handoff, shared custody production/consumption policy, pure role resolution
and an execution codec owning run-record serialization/comparison, and
cohesive SQLite schema ownership. No
abstraction finding is deferred. Publication, lifecycle preservation and
external-review completion remain later/parent-owned work.

## Owners and authority

`SqliteIssueRunLedger` owns allocation, capability lifetime, immutable submission
and validator-attestation tables, processing acknowledgements, and repair. Its
private `CompletionIntakeTables` implementation uses the same SQLite transaction
boundary and exact allocated run identity. The added tables are
`completion_intake_runs`, `completion_intake_entries`,
`completion_validation_attestations`, and `completion_intake_processing`.
The intake implementation does not alter disposition tables. The combined
checkpoint includes the separately reviewed store and escrow prerequisites.

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

## Merged store interface compatibility

The new admission-only `SqliteValidatedWorkIntakeStore` composes the existing
`DispositionDatabase.transaction`, `EvidenceAdmissionWriter.admit`,
`LineageClassifier`, and disposition row reader. It consumes typed
`EvidenceAdmission` / `AdmissionOutcome`, `ValidatedWorkAncestry`, and
`ValidatedWorkArtifactVerifier` boundaries. The merged store preserves these transaction and identity interfaces; the
combined admission, lineage, claim, retention, escrow and typed-port tests
exercise them alongside the historical intake owner.
It does not construct the full store, acquire publication claims, supply fake
liveness, or fabricate caller proofs. Artifact and ancestry checks use actual
local Git and content hashes.

The narrow adapter requires PARKED both on input and the resulting disposition
inside one transaction. Existing publishing/recovered records cannot be retargeted
by import. Changes to the independent store API should be reconciled at this
adapter/composition seam, not by weakening these checks.

## Terminal intake fixture audit at `629dff7`

Root's full gate exposed a manually constructed `ActionApplier` without its
required terminal intake owner. Production bootstrap already injects the same
real owner into the applier, lifecycle and processor. The bounded constructor
audit found the same gap in `test_actions` and terminal timeline fixtures.
There are **no production source, API, schema or quality-baseline changes** in
this repair; downstream preservation needs no production API propagation.

The shared typed fixture now owns a real `CompletionEvidenceIntakeService`,
SQLite ledger and configured `ValidationRunner`; only external ports are
substituted. Applier and lifecycle tests inject it explicitly. Accepted receipts
prove capability closure, queue drain and trusted attestation before session
stop. Corrupted custody proves escalation fails before session or label effects.
Unused processor fixture intake methods now fail loudly when invoked without
explicit injection, so future terminal paths cannot silently pass on a mock.

The broader audit also exposed seven raw `coding-done` producer failures in
`test_completion_command_contracts`: those subprocess fixtures supplied no
allocated capability or receipt endpoint. They now allocate through the real
run owner and submit through the existing real HTTP fixture. The end-to-end
human-routing case retains that same owner and allocated run through actual
receipt consumption. Context exit closes/drains in `finally`. Existing dirty
file, real-hook, schema and human-routing assertions remain intact. The shared
real-hook repository builder has one module-level fixture boundary.

Verification for this test-only repair:

- Initial 23-module constructor audit: **2 failed, 1144 passed**; both failures
  were missing applier fixture owners.
- Five directly affected lifetime/timeline modules after repair: **88 passed**,
  including safe and corrupted custody controls.
- Expanded 30-module audit: **7 failed, 1206 passed**, exposing the raw producer
  fixtures described above.
- Final serial run of the other 29 modules: **1192 passed** in 62 seconds;
  `/private/tmp/intake-629-socketfree-final.log`. Exact module list:
  `/private/tmp/intake-629-constructor-final-modules.txt` minus the raw command
  contract module named below.
- The complete raw command contract module remains **unverified after repair**:
  the sandbox rerun reached `socket.bind` and failed with `PermissionError`
  before invoking the first coder subprocess (3 earlier cases passed). Root must
  run `.venv/bin/pytest -q -n 0 tests/integration/test_completion_command_contracts.py --tb=short`
  outside the sandbox. No repository test was skipped or suppressed.
- Changed Python files pass Ruff; both shared intake fixture modules pass targeted
  Pyright. Production source and the original `a10bda5` quality baseline remain
  byte-for-byte unchanged. No full gate, live-provider run or remote write ran.

The same retained Gauss reviewer (`/root/intake_internal_reviewer`,
`gpt-6-astra`) reviews this exact fixture repair, including the final abstraction
pass. Its final verdict accompanies the checkpoint. Implemented abstraction
findings: one typed lifetime fixture across manual constructors, fail-fast
unused processor intake ports, and one allocated authority spanning subprocess
submission and receipt consumption. No abstraction finding is deferred.

The exact repair manifest is
`/private/tmp/techlead-intake-629-fixture-checkpoint.sha256`, bound to base
`629dff74bf90f6af75f7fca4d1ae1e811f106d08`. It supersedes the earlier three-file
combined-base manifest for this repair only; prior verification below remains
historical evidence, not a claim that the root full gate passed.

## Prior combined verification checkpoint

- Root's complete deterministic exchange integration run: **12 passed,
  1 live test deselected**, before the prerequisite merges.
  Log: `/tmp/techlead-intake-validation-repair-nonlive-integration.log`.
  That rerun resolves the previously reported seven validation-output failures.
- At committed combined base `2055cf3`, actual **`make lint-arch` passed**:
  10 import contracts kept, AST rules, the original quality baseline comparison,
  AGENTS checks and documentation checks all green. The inherited store metric
  is resolved; there is no remaining prerequisite-metric exception.
  Log: `/private/tmp/intake-combined-lint-arch.log`.
- Actual **`make typecheck` passed** at the same committed base: complete standard
  and strict modes each report **0 errors, 0 warnings**.
  Log: `/private/tmp/intake-combined-typecheck.log`.
- Combined focused suite: **1270 passed**. The prior 923-case suite was expanded
  with store/domain/ports, lineage,
  concurrency, claims, attempts, escrow, retention, validation containment and
  retry/revalidation modules. The exact combined command is retained in
  `/private/tmp/intake-combined-focused-command.txt`; its final result is in
  `/private/tmp/intake-combined-focused-final.log`.
- Extra exact-Git, configuration, settings and worktree compatibility checks:
  **567 passed**, recorded in `/private/tmp/intake-combined-adapter-config-final.log`.
- Compatibility testing exposed four containment tests that bypassed constructor
  injection with `CompletionProcessor.__new__`. They now construct the actual
  extracted `CompletionValidationArtifacts` owner and call its public `attach`
  method, preserving all containment, stale-copy and same-file assertions.
  The shipped maintenance-config test now loads identical YAML bytes in its
  temporary installation, so default directory creation stays outside the real
  checkout. No production source or configuration behavior changed.
  The two repaired modules pass **314 tests**; log:
  `/private/tmp/intake-combined-fixture-repair.log`.
- The same retained reviewer `/root/intake_internal_reviewer` (Gauss),
  **gpt-6-astra**, reviews this combined checkpoint under
  `repo-specific/prompts/internal-review.md`, including a final abstraction pass.
  Its final conversational verdict accompanies the checkpoint.
- No publication, CI, full gate, live-provider test, remote write, production
  restart, completion command or other-worktree mutation was performed.
  Root owns the separately scheduled full gate and urgent PR #7176 CI.

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
is byte-identical to the original `a10bda5` baseline. The complete combined
guardrail comparison is green;
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
The extracted pair-validation owner and persistence codec each measure 2. The
complete combined guardrail comparison, including semantic and typed-boundary
rules, passes against the original baseline. Import-linter reports **10 contracts kept, 0 broken**;
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
pending. Root subsequently ran the complete deterministic HTTP/subprocess module outside
the sandbox: all 12 cases passed, with the live-provider case deselected.

The repair changes four source/test paths relative to the approved 91-file
snapshot, plus this handoff: `control/completion_intake_validation.py`,
`control/validation.py`, `control/validation_record_store.py`, and
`tests/integration/test_completion_validation_custody.py`. Other reviewed files
retained their previous hashes at `71f53e5`. Root has now merged the separately
reviewed store/escrow prerequisites into the combined checkpoint.

## Remaining invariants and root sequencing

“Processed” acknowledges intake validation/historical admission, not exactly-once
publication. General disposition capture across all validated heads, lifecycle preservation,
exact-head publishing and Control Center recovery UI remain separate slices.
The escrow/retention primitives are merged; wiring their scheduling and
publication authority belongs to the later disposition owner. The generic
slice-2 escrow factory remains unscheduled and has its own envelope format and
root. Historical intake still uses bounded `historical-intake-escrow` custody;
this checkpoint does not establish generic escrow maintenance or publication
consumption of historical receipts. The legacy
operator manual publication recovery path is not migrated by this slice.
No implementation claim here permits publication from arbitrary historical JSON.
`require_publication_ready` checks receipt-owned attestation; it does not grant
lifecycle/publication authority. Later integration needs the proper behavior-level
owner port, with no caller-fabricated trusted validator results or liveness proofs.

Root owns named-file commits if sandboxed Git metadata prevents them, hook/index
authority, full gates scheduled one at a time, and independent external review
after internal approval. The deterministic subprocess rerun has passed. Do not mark #6914 complete from this checkpoint.

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

## Checkpoint identity and manifests

The earlier combined source base was `2055cf3740897f6bf5f6ed221c5a22d91b2c5402`.
Root committed the three-file compatibility update (this handoff,
`tests/unit/test_config.py` and `tests/unit/test_validation_record_containment.py`)
as `629dff7`. `/private/tmp/techlead-intake-combined-checkpoint.sha256` is therefore
historical. The current test-only repair manifest and its verification limits
are specified in the terminal intake fixture audit above.

The original 94-file intake implementation inventory below is historical: root
verified `/private/tmp/techlead-intake-validation-repair-files.sha256` before
committing `71f53e5`. The prerequisite merges legitimately changed some of these
files; that old manifest is not a hash claim for the combined checkpoint.

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
