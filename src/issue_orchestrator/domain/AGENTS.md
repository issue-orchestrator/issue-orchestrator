# Domain

**Purpose**: Core business concepts - identity types, models, state machines, business rules.

**Boundaries**:
- Infrastructure-agnostic: no GitHub, tmux, file paths, or external system concepts
- Only depends on Python stdlib
- Identity types (`SessionKey`, `IssueKey`, `TaskKind`) live here
- State machines define valid transitions, not how to execute them

## Run-Asset Value Objects

- Domain run-asset contracts such as `SessionRunAssets` and
  `ReviewExchangeRunAssets` must be frozen, slot-backed value objects with
  required fields.
- Required artifact paths must not be optional. Constructors should reject
  missing, relative, or cross-run paths immediately.
- Domain types may model filesystem path invariants, but they must not discover
  paths by scanning worktrees or external systems.
