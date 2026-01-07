---
name: architecture
description: Understand hexagonal architecture, ports/adapters pattern, dependency injection, and the composition root. Use when working on ports in ports/, adapters in execution/, bootstrap.py, or adding new external system integrations.
---

# Architecture

This skill provides context for working on the system architecture.

## When to Use

- Working on ports (Protocol interfaces) in `ports/`
- Working on adapters in `execution/`
- Modifying dependency injection or `bootstrap.py`
- Adding new external system integrations
- Understanding the layered architecture

## Key Resources

Read these files for context:
- [docs/architecture/README.md](docs/architecture/README.md) - Full architecture documentation
- `src/issue_orchestrator/ports/` - Port definitions (Protocols)
- `src/issue_orchestrator/execution/` - Adapter implementations
- `src/issue_orchestrator/bootstrap.py` - Composition root

## Core Principles

1. **Hexagonal Architecture** - Core defines Protocols (ports), adapters implement them
2. **DI via Constructor** - Orchestrator receives dependencies, doesn't create them
3. **Composition Root** - `bootstrap.py` is the only place that wires dependencies
4. **Three Layers**:
   - Observation (`observation/`) - Gathers facts, no decisions
   - Control (`control/`, `orchestrator.py`) - Makes decisions
   - Execution (`execution/`) - Talks to external systems

## Key Ports

| Port | Purpose | Adapter |
|------|---------|---------|
| `EventSink` | Fire-and-forget trace events | `PluggyEventSink` |
| `SessionRunner` | Terminal session management | `PluggySessionRunner` |
| `IssueTracker` | GitHub issue operations | `GitHubAdapter` |
| `SessionStore` | Persist session state | `JsonSessionStore` |
