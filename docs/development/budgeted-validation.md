# Budgeted validation

Budgeted validation is IO's workload for important tests whose cost makes them
unsuitable for every PR. Normal PR validation still runs deterministic tests.
Named budgeted suites run against an exact commit on their configured remote
branch, in isolated checkouts, with separate coverage history.

```yaml
validation:
  budgeted:
    live-agents:
      enabled: true
      command: [.venv/bin/python, -m, pytest, tests/integration, -m, live_agent, -p, scripts.agent_test_report]
      setup_command: [make, venv-fast]
      issue_agent_label: agent:backend
      branch: main
      timeout_seconds: 3600
      setup_timeout_seconds: 900
      cadence:
        max_merges_since_success: 10
        max_delay_hours: 24
```

The values come from the selected IO YAML configuration and can differ per
suite. Either threshold makes changed code due. A suite without a successful
baseline runs immediately. Unchanged code does not spend another run merely
because time passes. The delay is elapsed hours, not a calendar-day rollover.
Only successful **scheduled** runs advance coverage; baseline-verification and
bisection probes cannot make a red branch look green.

The merge count follows first-parent integrations. Standard squash subjects
`(#N)` and merge subjects `Merge pull request #N` count distinct PRs. Untagged
main commits count conservatively as additional integrations: direct commits or
unusual merge messages may cause an early run rather than defer detection.

The repository engine checks for due work asynchronously while running and
unpaused. One process-held lease in the common Git directory coalesces all
worktrees and config modes for that repository. Before scheduler submission,
the worker durably reserves the exact run identity and checkout. A restarted
worker reconciles that identity instead of submitting again. The scheduler
enforces both the active-runtime deadline and an absolute queue-to-cleanup
bound.
The reservation also owns the exact command, setup, branch, timeouts, and pool
identity that were submitted. A config edit, disable, or removal affects later
runs; it cannot rewrite or hide unfinished work. IO reconciles all unfinished
reservations before it admits a replacement definition with the same suite
name.
The configured freshness bound requires an active engine and available test
infrastructure. When those are unavailable, coverage remains overdue; it is
never recorded as a pass. A stopped engine cannot promise a detection deadline.

Live suites run only through the Linux execenv's cgroup-attested HTCondor pool.
Native macOS and ordinary process-group execution cannot contain a detached or
double-forked agent process, so those hosts return unavailable before launching
a provider. Loss of a submit acknowledgement leaves the reservation in place;
the next worker queries by its stable ClassAd identity and never guesses that it
may submit again. A terminal leader is insufficient for cleanup: IO retains the
checkout until the job has left the queue and the pool has written its final
per-job ClassAd, which is the scheduler's proof that the cgroup-owned family was
reaped.
The containment owner reads the pool's schedd and collector identity with
per-process scheduler overrides removed, pins every submit and query to those
names, and records that identity in the reservation. Each live-validation job
also requires the execenv's cgroup attestation in the target slot ClassAd. A
restart under a different pool refuses reconciliation rather than transferring
authority to the new scheduler.

## Results and diagnosis

Commands return 0 for success, 1 for a test failure, and 75 for unavailable
infrastructure/quota. Other abnormal exits and timeouts are unavailable.
Commands can write JSON to `IO_BUDGETED_VALIDATION_RESULT`:

```json
{"status": "failed", "failed": ["stable-test-identity"]}
```

The stable failing-test identities enable automatic bisection. Commands without
this evidence still produce a tracked failure, but IO cannot establish that
successive failures are the same regression and does not accuse a commit.
This repository's `scripts.agent_test_report` pytest plugin supplies the report.
It treats skipped/missing coverage and recognised provider quota failures as
unavailable, including all-skipped suites. Live execution is serial.

IO first reproduces the failure and verifies the last green endpoint under the
current environment. It then tests midpoint commits on the first-parent range.
For N suspect integrations, at most `ceil(log2(N))` midpoint runs are needed;
there is no configurable bisection depth. The two endpoint checks are additional.
Unavailable results, changed failure identities, or a failing old baseline leave
the diagnosis inconclusive and retain the original commit range.
Every reproduction, baseline, and midpoint reservation is part of the same
durable diagnostic sequence. If the coordinator exits, the next worker resumes
the exact pending probe and continues at the next uncompleted step without
replaying acknowledged probes or buying another scheduled run.

Red or unavailable coverage does not cause a run on every engine tick. Retry
spending is bounded by the same cadence, separately from the successful-coverage
baseline. An explicit `run` request can retry after fixing code or restoring
provider access.

A completed failure becomes a normal regression issue through observation,
planning, and action application. The issue includes the last green commit,
first observed failure, bisection result, evidence path, and a requirement to
prove the repaired suite green. The configured coding agent can work it, and the
tech lead owns following it through completion. Durable creation intent and
body-marker reconciliation prevent duplicate POSTs after ambiguous responses.
An unresolved creation remains visible in the action failure and local receipt;
IO will reconcile a late marker but will not blindly recreate the issue.

## Commands

From the repository checkout, using the active IO config selection:

```bash
make agent-test-status       # Read coverage without running tests
make agent-test-check        # Run only suites whose cadence is due
python -m issue_orchestrator.entrypoints.cli_tools.budgeted_validation run --suite live-agents
```

Pass `--config path/to/config.yaml` to select a specific YAML configuration.
`run` is an explicit forced request. `check` still coalesces with another worker.
`status` reports the scheduled coverage verdict separately from diagnosis probes.
`check` and `run` return 1 for failed coverage and 75 for unavailable coverage or
a busy worker; a successful midpoint cannot make those commands report green.
`make test-agent-live` runs this repository's whole live suite directly, useful
for development; it does not create an IO successful-coverage receipt.

History and evidence live under the repository's common Git directory in
`io-budgeted-validation`. Successful and failed test commands leave logs,
result JSON, and the durable containment receipt there; their temporary
worktrees are removed only after the final scheduler containment proof.
`make validate-pr` and its exact-HEAD cache remain exclusively deterministic.
A green PR validation receipt is not a live-agent coverage receipt.

Activation is a rollout step: merge this change, update/restart the executor's
IO installation inside the Linux execenv, provision provider credentials under
the execenv credential contract, and let the enabled suite establish its first
baseline. Other repositories opt in by declaring their own suites. No provider
credentials are installed by this feature.
