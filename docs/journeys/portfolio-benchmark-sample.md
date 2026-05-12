# Applied AI Portfolio Benchmark — Sample Output

This is a checked-in sample of the artifact produced by `make portfolio-benchmark`, so evaluators can read the proof bundle without cloning or running anything.

To regenerate locally, see [Portfolio Benchmarking](benchmarking.md). The benchmark itself runs in ~12s once dependencies are installed; the artifact also includes `summary.json`, `junit.xml`, the exact pytest command, and stdout/stderr alongside this markdown.

---

- Generated: `2026-05-12T15:12:38Z`
- Repo: `issue-orchestrator`
- Output dir: `.issue-orchestrator/portfolio-benchmark/latest`
- Overall status: `passed`
- Pytest exit code: `0`
- Cases: `10` (passed=10)
- Aggregate duration (reported by pytest): `7.051s`

## Claims

| Case | Status | Capability | Claim | Why It Matters |
| --- | --- | --- | --- | --- |
| happy_path_pr | passed | Deterministic local coder-reviewer loop | A bounded local review exchange can complete and produce a merge-ready PR. | Shows the system can convert an issue into reviewed code without hand-held agent orchestration. |
| draft_pr_review | passed | Structured draft-PR review workflow | The draft-PR review path applies approval labels and advances the issue state correctly. | Demonstrates that the orchestrator mediates review outcomes instead of treating agent output as self-authenticating. |
| review_rework | passed | Reviewer-driven rework loop | Changes-requested reviews can send work back for rework and later converge on approval. | This is the core applied-AI quality story: the system can absorb critique and recover, not just succeed on the happy path. |
| validation_retry | passed | Bounded validation retry | A failed validation step can retry once and still converge on a passing state. | Shows the orchestrator handles flaky or transient failure modes with controlled retries instead of silent loops. |
| publish_failure | passed | Failure classification and blocking | Publish failures are classified, surfaced, and leave the issue blocked instead of pretending success. | Hiring teams care about what happens when the model or environment is wrong; this demonstrates conservative failure handling. |
| needs_human | passed | Human-in-the-loop escalation | Agent-declared ambiguity becomes an explicit needs-human state with matching events and labels. | This makes the system credible as applied AI rather than 'full autonomy' theater. |
| reconciliation_pause | passed | External-state reconciliation | Label drift triggers reconcile-required events and pauses the issue instead of mutating stale state. | Real systems drift. This shows the control plane treats external coordination state as authoritative and fallible. |
| run_manifest | passed | Run-scoped diagnostics artifacts | Validation failures update the run manifest with explicit status and completion metadata. | Applied AI systems need replayable evidence, not hand-wavy logs. This proves the diagnostics contract is exercised. |
| restart_recovery | passed | Crash-safe restart recovery | Restarted orchestrators recover work from durable labels instead of relying on in-memory session state. | This is one of the strongest signals that the system was designed as infrastructure, not a demo script. |
| sqlite_backups | passed | Durable local state protection | Existing SQLite state is backed up automatically when backup cadence is enabled. | It shows long-term operational thinking around stateful AI workflows and recovery, not just prompt choreography. |

## Artifact Bundle

A live run writes the following alongside this file:

- `summary.json` — machine-readable report suitable for dashboards, resume snippets, or portfolio automation.
- `summary.md` — shareable benchmark summary for project pages or interview packets.
- `junit.xml` — raw pytest output for auditability.
- `pytest-command.txt` — exact command used to generate the bundle.
- `pytest-stdout.txt` / `pytest-stderr.txt` — execution logs for debugging failures.
