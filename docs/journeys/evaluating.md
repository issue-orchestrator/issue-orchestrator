# Evaluating the System

You're assessing Issue Orchestrator's design — whether the architecture is sound, the engineering is serious, and the system would hold up in production.

## Start here

**[README](../../README.md)** — The pitch and how-it-works diagram. Takes 2 minutes. Notice the agent-reviewer loop with cycle limits — that's the core quality enforcement mechanism.

**[REVIEWER_README](../../REVIEWER_README.md)** — Written for you. Explains what's stable (orchestration core, guardrails, workflow enforcement, resilience) versus what's experimental (planning, UI polish, some adapters). Focus your time on the stable areas.

## Architecture

**[Architecture diagram](../architecture/README.md)** — The hexagonal (ports and adapters) system diagram. Everything flows through Protocol interfaces — the core has no knowledge of GitHub, terminals, or storage implementations.

**[CLAUDE.md](../../CLAUDE.md)** — This is the project's conventions document. It's addressed to AI agents (the tone reflects that), but the architecture principles, fail-fast philosophy, event vs logging rules, and abstraction heuristics are the real engineering standards. Start at "Architecture Principles" and read through "Conventions."

## Guardrails — the key differentiator

This is the part worth scrutinizing. The thesis is: AI agents optimize for completion, so you need mechanical enforcement, not just prompt instructions.

**[Guardrails & Safety Model](../design/guardrails.md)** — What the system guarantees, what it doesn't claim, and why. Read the "What the system does not claim" section for intellectual honesty about limitations.

**[Hook Enforcement Architecture](../architecture/hooks.md)** — Three defense-in-depth layers (AI agent hooks, git hooks, server-side protection). Focus on the support matrix and the verification flow — hooks aren't just installed, they're tested.

## Architectural decisions

There are 24 [ADRs](../architecture/ADR/README.md). These five capture the core thinking:

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
| Orchestrator core | `src/issue_orchestrator/orchestrator.py` | Main facade, delegates to control/ |
| Decision logic | `src/issue_orchestrator/control/` | Scheduler, planner, action applier — all pure logic, no I/O |
| Port interfaces | `src/issue_orchestrator/ports/` | ~26 Protocol definitions — the abstraction layer |
| State machines | `src/issue_orchestrator/domain/state_machines/` | Issue and review lifecycle |
| Composition root | `src/issue_orchestrator/bootstrap.py` | Where dependencies are wired |

## Quality signals

- **~4000 unit tests** with import-linter enforcing architecture boundaries
- **Strict Pyright** on core modules, standard mode elsewhere
- **Fail-fast by default** — fallbacks are explicitly discouraged (see CLAUDE.md "Fail-Fast Design")
- **Events over logging** — structured trace events drive the UI, tests assert on events not log text
- **24 ADRs** documenting non-obvious decisions
