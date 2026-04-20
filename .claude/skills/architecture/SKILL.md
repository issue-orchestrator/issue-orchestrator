---
name: architecture
description: Understand hexagonal architecture, ports/adapters pattern, dependency injection, and the composition root. Use when working on ports in ports/, adapters in adapters/, execution support code, entrypoints/bootstrap.py, or adding new external system integrations.
---

# Architecture

This skill provides context for working on the system architecture.

## When to Use

- Working on ports (Protocol interfaces) in `ports/`
- Working on concrete adapters in `adapters/`
- Working on execution support code in `execution/`
- Modifying dependency injection or `entrypoints/bootstrap.py`
- Adding new external system integrations
- Understanding the layered architecture

## Key Resources

Read these files for context:
- [Architecture Overview](../../../docs/architecture/README.md) - Current architecture documentation
- `src/issue_orchestrator/ports/` - Port definitions (Protocols)
- `src/issue_orchestrator/adapters/` - Concrete external-system adapters
- `src/issue_orchestrator/execution/` - Runtime services and provider factories
- `src/issue_orchestrator/entrypoints/bootstrap.py` - Composition root

## Core Principles

1. **Hexagonal Architecture** - Core defines Protocols (ports), adapters implement them
2. **DI via Constructor** - Orchestrator receives dependencies, doesn't create them
3. **Composition Root** - `entrypoints/bootstrap.py` is the only place that wires dependencies
4. **Three Layers**:
   - Observation (`observation/`) - Gathers facts, no decisions
   - Control (`control/`, `infra/orchestrator.py`) - Makes decisions and coordinates
   - Adapters/Execution (`adapters/`, `execution/`) - External-system integration and runtime support

## Key Ports

| Port | Purpose | Adapter |
|------|---------|---------|
| `EventSink` | Fire-and-forget trace events | `PluggyEventSink` (in `execution/`) |
| `SessionRunner` | Terminal session management | `PluggySessionRunner` (in `execution/`) |
| `IssueTracker` | GitHub issue operations | `GitHubAdapter` (in `adapters/`) |
| `SessionStore` | Persist session state | `JsonSessionStore` (in `execution/`) |

These are the foundational ports. See `ports/` for the full set (~32 protocols).
