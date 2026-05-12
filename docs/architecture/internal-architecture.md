# Issue-Orchestrator Internal Architecture

This document describes how the Issue-Orchestrator codebase itself is built. It is separate from the product thesis in the README.

Issue-Orchestrator can enforce many different target-repo standards: hexagonal boundaries, package boundaries, UI/domain separation, service boundaries, coverage gates, or whatever a project encodes in validation and review. This repo happens to use hexagonal architecture internally because the orchestrator has to make decisions about untrusted agent output while talking to GitHub, terminals, storage, hooks, and UI clients.

## Internal Shape

- **Control plane:** `src/issue_orchestrator/control/` owns scheduling, planning, and action decisions. It should stay policy-focused and testable without external I/O.
- **Observation:** `src/issue_orchestrator/observation/` gathers facts. It does not decide policy or mutate external state.
- **Domain:** `src/issue_orchestrator/domain/` owns models, state machines, and lifecycle concepts.
- **Ports:** `src/issue_orchestrator/ports/` defines Protocol interfaces for external capabilities.
- **Adapters:** `src/issue_orchestrator/adapters/` implements concrete integrations, such as GitHub and local git behavior.
- **Execution support:** `src/issue_orchestrator/execution/` owns runtime support, provider factories, and session orchestration support code.
- **Composition root:** `src/issue_orchestrator/entrypoints/bootstrap.py` wires concrete implementations into the orchestrator.

## Why Hexagonal Architecture Matters Here

The orchestrator treats agent output as untrusted input. That means the code deciding whether work advances must be easy to test without depending on live GitHub state, terminal sessions, storage, or UI behavior.

The internal architecture supports that by keeping:

- decisions behind ports instead of concrete SDKs or shell calls
- adapters outside core policy
- dependency wiring in one composition root
- tests focused at behavior and port boundaries
- events structured for UI and test synchronization
- logs reserved for human debugging

The point is not that every target repo should use this architecture. The point is that this repo's own architecture is named, enforced, and testable, which is part of its engineering proof.

## Enforcement

Internal boundaries are enforced by:

- import-linter contracts in `pyproject.toml`
- custom AST guardrails in validation
- pre-push validation and CI
- ADRs for decisions that affect correctness, safety, boundaries, and extensibility
- tests that mock ports instead of patching internals

For the diagram-level overview, see [Architecture](README.md). For the safety model around agent work, see [Guardrails & Safety Model](../design/guardrails.md).
