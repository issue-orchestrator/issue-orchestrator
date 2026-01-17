# Domain

**Purpose**: Core business concepts - identity types, models, state machines, business rules.

**Boundaries**:
- Infrastructure-agnostic: no GitHub, tmux, file paths, or external system concepts
- Only depends on Python stdlib
- Identity types (`SessionKey`, `IssueKey`, `TaskKind`) live here
- State machines define valid transitions, not how to execute them
