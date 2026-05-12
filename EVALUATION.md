# Evaluating Issue-Orchestrator

A one-screen proof bundle for evaluators. For the full pitch, see [README](README.md). For a deeper walkthrough, see [Evaluating the System](docs/journeys/evaluating.md).

## Authorship

I designed and built Issue-Orchestrator as a solo project. I used coding agents the same way the system is designed to use them: as bounded contributors inside human-defined constraints. I owned the architecture, workflow design, guardrails, validation strategy, and final technical decisions. Agents produced code against my abstractions; I chose the abstractions, set the quality bar, caught bad output, and integrated the pieces into a coherent system.

## Proof bundle

| Claim | Evidence |
|-------|----------|
| Large automated test suite | `pytest tests/unit/ -q --timeout=60` reports the current unit-suite count; broader integration, e2e, and simulated-scenario suites live under [`tests/`](tests/) |
| Architectural decisions documented | ADRs in [`docs/architecture/ADR/`](docs/architecture/ADR/) |
| Internal hexagonal architecture, fully wired | [Internal Architecture](docs/architecture/internal-architecture.md); [ADR-0011](docs/architecture/ADR/0011-hexagonal-architecture.md); Protocol interfaces in [`src/issue_orchestrator/ports/`](src/issue_orchestrator/ports/) |
| Internal architecture boundaries enforced, not just documented | `import-linter` contracts in [`pyproject.toml`](pyproject.toml) + pre-push hooks |
| Engineering contract articulated | [No Free Lunch for Coding Agents](docs/journeys/no-free-lunch.md) explains the architecture, guardrail, validation, test, and human-authority contract around agent work |
| Mechanical guardrails, not prompt-based rules | [ADR-0012](docs/architecture/ADR/0012-mechanical-guardrails.md); multi-layer hook enforcement in [`docs/architecture/hooks.md`](docs/architecture/hooks.md) |
| Crash-safe state via GitHub labels | [ADR-0013](docs/architecture/ADR/0013-labels-as-crash-safe-truth.md); restart-recovery scenarios in [`tests/simulated_scenarios/`](tests/simulated_scenarios/) |
| Observe-Plan-Apply discipline | [ADR-0014](docs/architecture/ADR/0014-observe-plan-apply-loop.md); pure-logic decision layer in [`src/issue_orchestrator/control/`](src/issue_orchestrator/control/) |
| Shareable benchmark artifact | `make portfolio-benchmark` → [`.issue-orchestrator/portfolio-benchmark/latest/summary.md`](docs/journeys/benchmarking.md) |

## What this proves / doesn't prove

**Proves:**
- Architectural discipline (ports/adapters, fail-fast, documented decisions) applied end-to-end, not just in a sample module
- Mechanical safety: hooks block unsafe agent actions at multiple layers, rather than prompts asking the model to behave
- Recovery: the system reconstructs state from GitHub labels after a crash
- Solo delivery of a non-trivial engineered system with sustained quality discipline

**Doesn't prove:**
- Scale under production load with many concurrent teams
- Reliability of any specific AI model's output — the orchestrator is model-agnostic, so end-to-end quality depends on model choice, prompts, and the guardrails catching mistakes
- Team collaboration at scale — this was built solo. The collaboration signal visible here is how I work *with AI agents under my own constraints*, which is a narrower claim

## Quick evaluator path

**~30 seconds — generate the proof artifact bundle (primary):**

```bash
make portfolio-benchmark
cat .issue-orchestrator/portfolio-benchmark/latest/summary.md
```

Runs 10 deterministic simulated scenarios covering coder-reviewer completion, review rework loops, bounded validation retry, needs-human escalation, label-drift reconciliation, and restart recovery. The artifact is auditable: `summary.json`, `junit.xml`, and the exact pytest invocation are all written alongside the markdown summary.

**~90 seconds — verify the full unit test suite (secondary):**

```bash
pytest tests/unit/ -q --timeout=60
# expected: the unit suite passes
```

## Deeper

- [Making Agentic Development Sustainable](docs/design/sustainable-agentic-development.md) — design essay; why the system exists and the three engineering tracks
- [No Free Lunch for Coding Agents](docs/journeys/no-free-lunch.md) — concise thesis: the issue runner is not the product; the engineering contract is
- [Applied AI Evaluation](docs/journeys/applied-ai.md) — how to frame the project for hiring conversations
- [Evaluating the System](docs/journeys/evaluating.md) — architecture walkthrough, guardrails, where to read code
- [Portfolio Benchmarking](docs/journeys/benchmarking.md) — full benchmark documentation
