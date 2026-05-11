# Applied AI Evaluation

You want to evaluate Issue Orchestrator as an applied-AI system, or use it as a stronger portfolio artifact when talking to hiring managers.

The right framing is not "it uses multiple agents." The stronger framing is:

- a control plane for long-running AI-assisted software work
- an executable engineering contract around agent output
- deterministic workflow enforcement around untrusted model output
- architecture-centric artifacts: named boundaries, guardrails, tests, validation records, and ADRs
- durable state recovery when processes, labels, or providers drift
- human escalation instead of pretend autonomy
- replayable evidence for debugging and evaluation

If you keep that framing, the repo reads like applied AI engineering rather than agent-demo theater.

## Start with proof

The fastest high-signal path is:

1. Read the [README](../../README.md) for the product pitch and UI surface.
2. Read [No Free Lunch for Coding Agents](no-free-lunch.md) for the engineering-contract thesis.
3. Read [Making Agentic Development Sustainable](../design/sustainable-agentic-development.md) for the longer design narrative.
4. Run the [portfolio benchmark](benchmarking.md) to generate a shareable artifact bundle from deterministic simulated scenarios.
5. Use the dashboard timeline, session replay, and live E2E runner as supporting evidence.

The benchmark is the baseline proof. The UI and live workflows are the demo material that make the benchmark feel real.

## What to emphasize

### Reliable orchestration, not just prompting

Issue Orchestrator treats agent output as untrusted input. Agents express intent through structured completion records, while the orchestrator validates, reviews, reconciles, and decides what happens next.

That is the core applied-AI story: the system is designed around model fallibility.

### Recovery and reconciliation

The most credible parts of the system are the ones that assume failure:

- labels are crash-safe external truth
- review and validation loops are bounded
- restart recovery uses durable state instead of memory
- needs-human and blocked states are first-class outcomes

Teams hiring for applied AI care about these details because real deployments fail in exactly these ways.

### Observability as a product feature

The dashboard timeline, run manifests, event contracts, diagnostics artifacts, and session replay make the system inspectable. That matters because applied AI work is hard to trust when you cannot reconstruct why a run succeeded or failed.

## A good 90-second demo

Show the system in this order:

1. README thesis and architecture/quality contract.
2. The benchmark output at `.issue-orchestrator/portfolio-benchmark/latest/summary.md`.
3. The dashboard timeline for one issue.
4. Session replay for a run artifact.
5. One example of a failure or escalation path.

That sequence works because it starts with claims, then proves them, then shows the operator experience.

## Resume and Project Page Copy

Use claims that are concrete and falsifiable.

### Resume bullets

- Built a control plane for AI-assisted GitHub issue execution with isolated worktrees, structured completion contracts, review/rework loops, and replayable run artifacts.
- Designed the system around model fallibility: validation gates, human escalation, crash-safe recovery from GitHub labels, and deterministic reconciliation before state mutation.
- Encoded architecture-centric engineering standards through ports/adapters boundaries, guardrails, validation records, ADRs, and test suites that make agent output reviewable.
- Added an applied-AI benchmark path using deterministic scenario tests and shareable artifact bundles to demonstrate workflow reliability beyond the happy path.

### Project page summary

Issue Orchestrator is a reliability layer for AI coding agents. It turns GitHub issues into bounded, reviewable execution runs, isolates work in dedicated worktrees, enforces validation and code review, and recovers from failure using durable external state. The project is intentionally opinionated about human oversight, observability, and failure handling because those are the difference between a demo and a deployable applied-AI system.

## Interview themes worth leaning into

- Why "agent autonomy" is the wrong optimization target for serious engineering work.
- Why deterministic workflow and mechanical guardrails matter more than prompt cleverness.
- What external truth and replayable artifacts buy you when debugging AI systems.
- How you chose abstractions that let policy stay testable and infrastructure stay replaceable.

## What not to claim

Avoid these:

- "fully autonomous software engineering"
- "agents can safely ship code on their own"
- "the benchmark proves production quality"

The honest claim is stronger: this repo shows how to build control, recovery, and observability around unreliable model behavior.

## Next supporting reads

- [Portfolio Benchmarking](benchmarking.md)
- [Evaluating the System](evaluating.md)
- [No Free Lunch for Coding Agents](no-free-lunch.md)
- [Making Agentic Development Sustainable](../design/sustainable-agentic-development.md)
- [Guardrails & Safety Model](../design/guardrails.md)
