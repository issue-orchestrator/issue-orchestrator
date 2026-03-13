# ADR 0011: Hexagonal architecture (ports and adapters)

**Status:** Accepted
**Date:** 2024-12-21

## Context

The orchestrator must interact with multiple external systems (GitHub API, terminal emulators, file system) while keeping core logic testable and maintainable. Direct coupling to external systems makes testing difficult and creates vendor lock-in.

## Decision

Adopt hexagonal (ports and adapters) architecture:

1. **Ports** (`ports/`) - Protocol interfaces defining what the system needs
   - `EventSink` - fire-and-forget trace events
   - `SessionRunner` - terminal session management
   - `IssueTracker`, `PullRequestTracker`, `LabelSet` - GitHub operations
   - `WorkingCopy` - local git operations

2. **Adapters** (`adapters/` with runtime support in `execution/`) - Concrete implementations of ports
   - `GitHubAdapter` - implements GitHub-related ports
   - `PluggySessionRunner` - delegates to terminal plugins
   - `GitWorkingCopy` - implements local git operations

3. **Core** (`control/`, `infra/orchestrator.py`) - Business logic using only ports
   - No imports from `adapters/`
   - No knowledge of concrete implementations
   - Receives dependencies via constructor injection

4. **Composition Root** (`entrypoints/bootstrap.py`) - Single place that wires dependencies
   - Only place that imports both ports and adapters
   - Creates concrete adapters and injects into orchestrator

## Consequences

### Positive
- **Testable**: Unit tests inject mock ports, no external systems needed
- **Swappable**: Change GitHub → GitLab or tmux → iTerm2 without touching core
- **Clear boundaries**: Import linter enforces layer separation
- **Maintainable**: Core logic isolated from infrastructure concerns

### Negative
- More files/indirection than direct coupling
- New developers must understand the pattern
- Protocol definitions add boilerplate

## Enforcement

- `import-linter` rules prevent core from importing adapters
- AST guardrails in pre-commit hooks
- Code review for boundary violations
