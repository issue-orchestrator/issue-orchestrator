# Portfolio Benchmarking

This repo includes a deterministic benchmark path aimed at evaluation, demos, and job-search packaging.

It does **not** try to prove that the orchestrator is production-perfect. It does give you a repeatable way to show that the core workflow handles success, review, rework, escalation, reconciliation, diagnostics, and restart recovery in a testable way.

The benchmark complements the project-specific engineering contract. It proves selected orchestration behavior; it does not replace architecture checks, validation commands, coverage gates, or human review criteria for a target repo.

## Quick start

From the repo root:

```bash
make portfolio-benchmark
```

That writes a benchmark artifact bundle under:

```text
.issue-orchestrator/portfolio-benchmark/latest/
```

Open the summary:

```bash
cat .issue-orchestrator/portfolio-benchmark/latest/summary.md
```

If you want a dated output directory:

```bash
make portfolio-benchmark \
  ARGS="--output-dir .issue-orchestrator/portfolio-benchmark/YYYY-MM-DD"
```

## What the benchmark covers

The benchmark is built on selected deterministic scenarios from [`tests/simulated_scenarios/test_simulated_scenarios.py`](../../tests/simulated_scenarios/test_simulated_scenarios.py). The current suite focuses on claims that matter for applied AI:

- deterministic coder-reviewer completion
- draft-PR review outcomes
- reviewer-driven rework loops
- bounded validation retry
- publish failure classification
- needs-human escalation
- label-drift reconciliation
- run-scoped diagnostics artifacts
- restart recovery from durable labels
- SQLite backup behavior for local state

To inspect the exact case ids:

```bash
python scripts/run_portfolio_benchmark.py --list
```

To run only a subset:

```bash
python scripts/run_portfolio_benchmark.py \
  --case happy_path_pr \
  --case review_rework
```

## Artifact bundle

Each run writes:

- `summary.json` for automation or dashboards
- `summary.md` for humans, project pages, and interview packets
- `junit.xml` as the raw pytest source
- `pytest-command.txt` with the exact invocation
- `pytest-stdout.txt` and `pytest-stderr.txt` for debugging

This is deliberate. A portfolio artifact should be auditable, not just presentational.

## How to use the results

The strongest pattern is:

1. Run the benchmark and keep `summary.md`.
2. Pair it with one screenshot or short recording of the dashboard timeline.
3. Pair it with one failure or escalation example.
4. Use those three artifacts together in your project page or interview walkthrough.

That combination tells a better story than a feature list because it demonstrates claims, operator visibility, and failure handling.

## What this benchmark is not

- It is not a substitute for the live E2E suite in [`docs/user/e2e.md`](../user/e2e.md).
- It is not a benchmark of model quality or token efficiency.
- It is not a measure of end-user business value.

It is a reliability-oriented benchmark for the orchestration layer.

## Stronger proof after the benchmark

Once the deterministic suite is green, the best next evidence is:

- a live E2E run from the [Async E2E Test Runner](../user/e2e.md)
- a timeline screenshot or session replay clip from the dashboard
- one short write-up of a failure mode and how the system handled it

That gives you benchmark proof plus operational proof.
