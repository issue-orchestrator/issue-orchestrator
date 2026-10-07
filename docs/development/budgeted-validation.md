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
python -m issue_orchestrator.entrypoints.cli_tools.budgeted_validation run --suite tech-lead-exam
```

This repository also runs the live **tech-lead exam** (`tech-lead-exam`, #7304) as a
budgeted suite. It covers five cases: A, B, C and D plant known faults, and U is an
upgrade with work in flight, run from `HEAD~10`, the span one cadence covers. Each
case runs the real engine and tech lead against GitHub and grades the outcome, so a
regression in how the tech lead handles a known fault is found within ten merges
and narrowed to the merge that caused it. Run a single case by hand with
`make test-tech-lead-exam EXAM_CASE=<A|B|C|D|E|U> EXAM_ENGINE_REF=<ref>`.
Scorecards are written under the repository's common Git directory, in
`io-tech-lead-exam/` (override with `EXAM_OUT=<dir>`), so they outlive the
suite's temporary checkout; the tech-lead improver (#7490) stages the latest
two of each case from there.

The **tech-lead improver** (`tech-lead-improver`, #7490) is a budgeted suite in
every mode, `enabled: false` until the operator turns it on. Daily (and after
50 merges at most), `make tech-lead-improver` audits the engine, stages the
improver's inputs, runs `examples/prompts/tech-lead-improver.md` read-only,
validates the findings strictly and records the run under
`<git common dir>/io-improver`. The agent's provider and model are pluggable
(#8001): `IMPROVER_PROVIDER=claude|codex` and `IMPROVER_MODEL=<model>` (the
CLI's `--provider` and `--model`). Unset, the improver runs on the latest
improver tournament's winner, Claude Opus (`claude -p --restricted` with only
the read tools, the prompt on stdin); each run records which it used.

**Empowered mode** (the default, #8001; `IMPROVER_MODE=scripted` gives the
agent the staged bundle alone). The staged `improver-data/` is the agent's
starting map; beside it the run stages a read-only toolbox,
`<run dir>/toolbox/`: byte copies of every engine store and of the engine's
logs, and a `--no-hardlinks` clone of the audited repository. The
orchestrator serves three tools to the agent as an MCP server on 127.0.0.1,
gated by a per-run bearer token passed in the agent's environment:
`github_get` (GitHub `GET` of the audited repository only; the credential
stays in the orchestrator), `sql_query` (SELECT and schema pragmas on a
store copy, under an SQLite authorizer: no write, `ATTACH` or extension) and
`git` (read-only subcommands in the clone; options that write a file, run a
program or read one outside it are refused, abbreviations included). The
agent has no shell and no write tool, and is told its budget
(`--budget-minutes`, default 60) to choose its depth in. Every toolbox call
is logged to `<run dir>/toolbox-calls.jsonl`. A blind run (`--exclude-open-issue`) of an engine that works the
outputs repository itself also hides those issues from `github_get`: a direct
read, or any answer that holds one, is refused. Git output is bounded while
it streams, and a SQL query runs under SQLite's own size limits. The Codex
agent's shell reads only its run directory (and the platform's runtime files). `make test-improver-escape`
runs a live agent ordered to break out of each boundary.

**Design findings** (`design_findings`, findings schema v5, #8001) report
where the system's model of the world doesn't match reality (a mechanism
with two meanings, a silent assumption, a manual operator step, a missing
capability, an operator-only friction), beside the stall findings. They are
held to the same evidence rule, checked mechanically: each citation quotes a
line of a staged file under `improver-data/` or `toolbox/` (never outside
them, symlinks resolved) or a toolbox answer by its call number (answers are
kept in `<run dir>/toolbox-answers/`). Only data counts: a `github_get`
answer, or a `sql_query` value present byte for byte in the queried store
copy; a `git` answer never counts (its `--format` is the request's). A quote that isn't there rejects the
whole file. An accepted design finding files one issue labelled
`needs-operator-decision` and `improver:design`; nothing is applied.

**Operator interventions** (`interventions.json`, #8001) also stage the hand
actions on the audited repository's GitHub, read in bounded pages over the
window: labels a person added or removed, title and body edits, items
opened, closes, reopens, merges, reviews and comments. Automation (a Bot
account or an App acting for a user) is left out and counted. Each entry
names its actor and a GitHub URL. Because the coordinator acts under the
operator's identity, an entry is attributed `coordinator` only when its
text carries the coordinator's signature, and `person` otherwise; the file
says so in `github.attribution_limits`, along with which sources stopped
before the window's start. With `--no-github`, or a failed read, the file
names what it is missing; staging never fails on it.

**Heats** (#8001): a run sends `--heats` independent agent runs
(`IMPROVER_HEATS`, default 2) on the same staged inputs and toolbox,
`--parallel-heats` at a time (default 2). Each is a whole agent run, and a
Claude heat counts against the operator's subscription, so keep N modest.
Each heat's answer is validated alone (`improver-findings-h<k>.json`); the
accepted ones are merged into `improver-findings.json`, which is validated
again. Findings merge by ONE identity, the key their issues are deduplicated
by: the same key is the same finding (a design finding only with the same
claim too). What cannot merge (a different finding proposing a taken exam
case, the same design id with another claim) is recorded as a conflict and
listed on the kept finding's issue, never dropped. The run records each
heat's outcome and, per finding, the heats that found it; a filed issue says
"Found by k of N independent heats". A run is accepted when any heat is. At
most 5 heats; their waves (`ceil(heats / parallel-heats)`) times
`--agent-timeout-minutes` must fit `--run-budget-minutes` (default 105,
inside the suite's 120).

**The improver tournament** (#8001;
`python -m issue_orchestrator.entrypoints.cli_tools.improver_tournament`)
compares improver arms (a provider, model and mode) on equal ground. All its
data lives under `<git common dir>/io-improver/`:

- `snapshots/<id>/` holds **frozen snapshots**: one engine's staged
  `improver-data/` and, optionally, its toolbox (store copies, logs, a clone).
  A snapshot is imported once (`snapshot import`) and never changed. It links
  to nothing outside itself and holds nothing from after it: the clone is
  rebuilt as a new repository from its refs (nothing else of its `.git` is
  kept), no ref may reach a later commit, and the checkout is its commit's. An old bundle is upgraded to today's contracts
  with each change named.
- `keys/<snapshot>.json` holds **answer keys from hindsight**: the sealed key
  written before results (`key seed`; no arm runs until it exists), then problems found later that the
  snapshot's evidence already showed (`key add`, a `candidate` until
  `key confirm`). Only these commands write keys. No improver run can reach
  the key store, and no agent can read it.
- `tournaments/<id>/` holds one **tournament**. `run` sends each arm's heats (an arm may set its own prompt, heats, budget
  and timeout: a challenger beside its champion) on the snapshot as ordinary improver runs, which never apply or touch GitHub,
  and read no live GitHub, since that would show what was found later.
  `grade-recorded` grades answers an earlier tournament recorded. Either way
  the outputs are anonymized (`anon/`, mapping sealed in `sealed/`), graded by
  cross-model graders (default one Claude, one Codex) that read only `anon/`
  and `key/` (every grader must grade every output, or there is no result;
  `regrade --tournament ID` grades the same outputs again), and ranked: weight × (full 1, half ½, miss 0), less 1 per
  unsupported finding. Each grader grades every output `--passes` times
  (default 3; graders side by side, a grader's passes in turn); a grader's
  passes are averaged first, since repeating one grader's opinion is not more
  evidence. An arm's score is the mean over its outputs and graders. Two arms
  are told apart only when their means differ by more than twice the standard
  error of the difference: the graders' disagreement on that difference, plus
  the heats' spread (never below the measured pass-to-pass noise). Arms within
  that band are reported indistinguishable (`≈`, grouped only when pairwise
  so), which is a result, not a failure. `result.json` records each grading,
  the pass noise, the distinguishable pairs and the cost (arm heats and every
  grader call, retries included, per provider; Claude's count against the operator's subscription).

**A run is dry unless `--apply`** (`IMPROVER_APPLY=1`): an accepted run's
GitHub effects are recorded as owed, and `improver apply` files them. A rejected findings file exits 1 with every
broken rule recorded and changes nothing; an unavailable input or agent exits
75. The accepted findings' effects (an issue per finding, deduplicated against
open issues by the `[improver:<key>]` title token, or an evidence comment on a
tracked issue; proposals labelled `needs-operator-decision`, never applied) go
through the repository-host port; a rate limit leaves the rest pending for the
next run. `python -m issue_orchestrator.entrypoints.cli_tools.improver status`
prints the runs, their stall-point grades and how those moved;
`... improver apply --outputs-repo <owner/repo>` applies what is still owed.
Point `IMPROVER_STATE_DIR` and `IMPROVER_AUDITED_REPO` at another engine (e.g.
porchpin) to audit it while filing into this repository.

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
