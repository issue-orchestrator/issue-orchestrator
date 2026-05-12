# Architectural Decision Records (ADRs)

These ADRs capture the *few* architectural decisions that materially affect correctness, security, boundaries, and extensibility.

**Rules**
- ADRs are append-only: do **not** rewrite history. If a decision changes, add a new ADR that **supersedes** an older one.
- Keep ADRs short (aim for ~1 page).
- Prefer decisions that prevent architectural drift over “nice-to-have” notes.

## Reading ADRs

ADR files live in this directory and are sorted by their numeric prefix. This README is not an exhaustive index; keeping a complete hand-written list here creates a second source of truth that can drift from the filesystem.

For the complete set, browse the `docs/architecture/ADR/` directory or list the files:

```bash
ls docs/architecture/ADR/[0-9][0-9][0-9][0-9]-*.md
```

## Core Starting Points

These ADRs capture the core architecture and safety story:

- [0011 Hexagonal architecture (ports and adapters)](0011-hexagonal-architecture.md)
- [0012 Mechanical guardrails over policy documents](0012-mechanical-guardrails.md)
- [0013 GitHub labels as crash-safe source of truth](0013-labels-as-crash-safe-truth.md)
- [0014 Observer → Planner → ActionApplier loop pattern](0014-observe-plan-apply-loop.md)
- [0016 Orchestrator as mediator (agents never touch GitHub)](0016-orchestrator-as-mediator.md)
- [0019 Structured completion protocol (agent-done)](0019-agent-done-completion-protocol.md)
- [0023 Deterministic orchestrator required (cannot replace with agentic-first solution)](0023-deterministic-orchestrator-required.md)
