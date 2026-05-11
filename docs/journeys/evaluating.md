# Evaluating the System

You're assessing Issue Orchestrator's design — whether the architecture is sound, the engineering is serious, and the system would hold up in production.

## Start here

**[No Free Lunch for Coding Agents](no-free-lunch.md)** — The concise thesis. The issue runner is not the main point; the engineering contract around agent work is the product.

**[Making Agentic Development Sustainable](../design/sustainable-agentic-development.md)** — The design essay. Why the system exists, the three tracks (enforceable architecture, deterministic workflow, externalized management), and lessons learned. 10 minute read.

**[Applied AI Evaluation](applied-ai.md)** — How to frame the repo for hiring conversations, what evidence matters most, and how to demo it without overselling autonomy.

**[Portfolio Benchmarking](benchmarking.md)** — Deterministic scenario-based benchmark path that emits a shareable markdown and JSON artifact bundle.

**[README](../../README.md)** — The pitch, concrete lifecycle, and project quality contract. Takes 2 minutes. Notice that agent output is treated as untrusted input; validation, review, and publish gates decide whether it moves forward.

**[Project Status](../../README.md#project-status)** — Current maturity statement. What is stable versus still evolving.

## Architecture

This section is about how the Issue-Orchestrator repo itself is built. It is implementation proof, not a claim that every target repo must use the same architecture.

**[Issue-Orchestrator Internal Architecture](../architecture/internal-architecture.md)** — The clearest separation of product thesis from implementation architecture.

**[Architecture diagram](../architecture/README.md)** — The hexagonal (ports and adapters) system diagram. Everything flows through Protocol interfaces, so orchestrator policy can be tested without GitHub, terminals, storage implementations, or UI clients.

**[AGENTS.md](../../AGENTS.md)** — This is the maintained contributor guide. It captures the engineering rules that shape the codebase: fail-fast design, event vs log boundaries, DI, and abstraction heuristics.

The artifact to evaluate is not only the diagram. Look for the implementation contract around it:

- Protocol ports and adapter boundaries
- import-linter and custom AST guardrails
- validation records tied to the current commit
- tests that mock at port boundaries instead of patching internals
- ADRs that explain why the boundaries exist
- structured events and generated contracts that keep UI/tests off log parsing

## Guardrails — the key differentiator

This is the part worth scrutinizing. The thesis is: AI agents optimize for completion, so you need mechanical enforcement, not just prompt instructions.

**[Guardrails & Safety Model](../design/guardrails.md)** — What the system guarantees, what it doesn't claim, and why. Read the "What the system does not claim" section for intellectual honesty about limitations.

**[Hook Enforcement Architecture](../architecture/hooks.md)** — Three defense-in-depth layers (AI agent hooks, git hooks, server-side protection). Focus on the support matrix and the verification flow — hooks aren't just installed, they're tested.

## Architectural decisions

The [ADR index](../architecture/ADR/README.md) captures decisions that materially affect correctness, security, boundaries, and extensibility. These five capture the core thinking:

| ADR | Why it matters |
|-----|----------------|
| [0011 — Hexagonal Architecture](../architecture/ADR/0011-hexagonal-architecture.md) | The foundational structural decision |
| [0013 — Labels as Crash-Safe Truth](../architecture/ADR/0013-labels-as-crash-safe-truth.md) | How the system recovers from failures |
| [0014 — Observe-Plan-Apply Loop](../architecture/ADR/0014-observe-plan-apply-loop.md) | The orchestrator's decision cycle |
| [0019 — Agent-Done Completion Protocol](../architecture/ADR/0019-agent-done-completion-protocol.md) | The trust boundary between agents and orchestrator |
| [0012 — Mechanical Guardrails](../architecture/ADR/0012-mechanical-guardrails.md) | Why enforcement beats documentation |

## Where to start reading code

| Area | Entry point | What to look for |
|------|-------------|------------------|
| Orchestrator core | `src/issue_orchestrator/infra/orchestrator.py` | Main facade, delegates to control/services |
| Decision logic | `src/issue_orchestrator/control/` | Scheduler, planner, action applier — all pure logic, no I/O |
| Port interfaces | `src/issue_orchestrator/ports/` | Protocol definitions — the abstraction layer |
| State machines | `src/issue_orchestrator/domain/state_machines/` | Issue and review lifecycle |
| Composition root | `src/issue_orchestrator/entrypoints/bootstrap.py` | Where dependencies are wired |

## Quality signals

- **Large automated test suite** with import-linter enforcing architecture boundaries
- **Validation contract** that can combine tests, linting, typing, coverage, architecture checks, and repo-specific policy scans
- **Strict Pyright** on core modules, standard mode elsewhere
- **Fail-fast by default** — fallbacks are explicitly discouraged (see AGENTS "Fail-Fast Design")
- **Events over logging** — structured trace events drive the UI, tests assert on events not log text
- **Architecture decision records** documenting non-obvious decisions
