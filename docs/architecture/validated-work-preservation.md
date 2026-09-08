# Validated work preservation at runtime boundaries

The admission-only runtime implementation of [ADR-0035](ADR/0035-validated-work-disposition.md) preserves validated work as `PARKED`. Publication and recovery approvals remain separate capabilities described by the [accepted design](../design/validated-work-disposition.md).

## Ownership and scope

`IssueRuntimeLifecycleOwners` is the shared boundary for termination, reset refusal, completion, cleanup, and shutdown. Its `CoreIssueRuntimeOwners` instance contains the visible session registry, review exchange pair, supervised exchange jobs, and publish retry owner. `OtherRuntimeActivity` exposes that same core. The full activity probe additionally observes every unresolved validated-work state.

Preservation scope follows the resource being destroyed:

- Issue termination and reset close all allocated runs for the issue.
- Completion and exact terminal cleanup close only the selected, ledger-owned runs.
- Worktree deletion captures every recorded run in that exact worktree, including background exchange runs.
- A stale generation target returns before intake closure or replacement-session mutation.

Allocation records the agent label and effective completion task with the attached branch and actual terminal binding. One SQLite codec retains all four facts, including explicit unknown values for legacy rows. Live session reconciliation compares the observed run/branch/terminal; effective completion role comes only from the durable allocation, never from current settings. Terminal binding is recorded independently of artifact phase names such as `coding-2`. A `RunTerminalBinding(None)` explicitly identifies a background allocation without a standalone visible terminal. Missing bindings in older SQLite rows remain unknown; migration does not invent them. Named-terminal selection refuses unknown ownership. Allocation and live evidence reconciliation use the same durable run ledger and active-session collection.

## Capture and admission

The completion intake owner closes and drains the selected scope, repairing acknowledged receipt artifacts before returning typed prepared candidates. Eligibility uses immutable owner-normalized completion bytes and validator attestations. Agent-authored sidecars and latest-run filesystem discovery grant no authority.

Manual consumers use `prepare_receipt_for_issue(receipt, run, issue_number)`. The intake owner verifies the full receipt/run and numeric issue binding before processing under the same drain lock, then returns `PreparedCompletionEvidence` with its immutable `CompletionRunRole`. The existing `prepare_receipt` shares the same preparation policy. Historical allocations retain the explicit `operator:historical` code role; it cannot represent review or Tech Lead authority.

Before teardown, escrow reconciliation repairs interrupted captures and verifies retained database rows against their envelopes and pins. Each distinct validated work key receives immutable escrow, an exact validated-head pin, and a separate observed-head pin when those heads differ. Advancing or detaching the checkout preserves both facts and leaves work parked with the corresponding failure reason. Missing objects or corrupt custody abort destructive continuation.

Automatic capture, fresh historical intake, and orphan replay share `ParkedEvidenceCustody` and `RankedEvidenceAdmission`. The latter reconstructs exact evidence identities through the intake ledger to obtain trusted global receive order. It conditionally admits against the current evidence identity inside a SQLite write transaction, retrying selection after a concurrent change. Capturing an older receipt later retains it as superseded evidence; it cannot replace a newer receipt for the same work key. Unmappable evidence fails closed. Capture and admission retries retain the original immutable envelope and converge after partial progress.

## Results and verification

Termination results require a `ValidatedWorkDispositionBatch`. Lifecycle observations and reset refusals expose every member's record, evidence, head, state, and failure. A reset refuses while any member remains unresolved. Ordinary termination can release runtime owners after custody is secured.

An unchanged detached reviewer checkout follows its existing cleanup path only after ownership and clean-tree checks plus proof that its commit remains reachable from a surviving parent branch. Unknown publishing-worktree ownership does not receive that authority.

The focused preservation tests exercise real Git objects, SQLite, filesystem escrow, independent admission instances, orphan replay, migration, and destructive-boundary refusals. Simulated completion flows use real trusted receipt intake and linked worktrees sharing the repository's object store.
