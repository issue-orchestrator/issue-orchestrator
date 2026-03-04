# Developing

You're modifying Issue Orchestrator — adding features, fixing bugs, or extending the system.

## Dev setup

```bash
git clone https://github.com/BruceBGordon/issue-orchestrator.git
cd issue-orchestrator
make venv
source .venv/bin/activate
pytest tests/unit/ -x -q    # Verify everything works (~4000 tests)
```

If you're working on a feature branch, use a git worktree to keep the base repo clean:

```bash
git worktree add ../issue-orchestrator-wt-my-feature -b my-feature
cd ../issue-orchestrator-wt-my-feature
make worktree-setup
```

## Understand the architecture

**[Architecture diagram](../architecture/README.md)** — The hex diagram shows how everything connects: entry points, control plane, ports, adapters, external systems.

**[CLAUDE.md](../../CLAUDE.md)** — This is the single source of truth for conventions. It's written for AI agents (so the tone is directive), but the rules apply to all contributors. The key sections:

| Section | What you'll learn |
|---------|-------------------|
| Architecture Principles | Hexagonal, DI, layered separation, labels as truth, agent intent vs orchestrator authority |
| Key Ports | The 4 foundational Protocol interfaces (and a pointer to the full ~26 in `ports/`) |
| Events vs Logs | Structured events drive the UI; logs are for humans. Never parse log text in code. |
| Fail-Fast Design | No fallbacks, no silent degradation. Crash on unexpected state. |
| Conventions | Where ports live, where adapters live, how to test, how to emit events |

## Find what you need to change

The codebase follows strict layered separation:

| Layer | Directory | Responsibility |
|-------|-----------|----------------|
| **Control** | `control/` | Decisions, policy, state advancement. Pure logic, no I/O. |
| **Observation** | `observation/` | Gather facts. No decisions, no mutations. |
| **Execution** | `execution/` | Talk to external systems. No policy. |
| **Ports** | `ports/` | Protocol interfaces. Contracts between layers. |
| **Domain** | `domain/` | Models, state machines, events. |

If you're adding a new external integration, you'll add a Protocol in `ports/` and an adapter in `execution/`. If you're changing decision logic, that's `control/`. The orchestrator (`orchestrator.py`) delegates to control — it shouldn't contain policy itself.

## Testing

```bash
pytest tests/unit/ -v              # Full unit suite
pytest tests/unit/test_foo.py -v   # Single file
pytest tests/e2e/ -v               # E2E tests (requires gh auth)
```

**Mock at port boundaries, not internal functions.** Create a mock that implements the Protocol, inject it, test the control logic in isolation. See [Testing Guide](../development/TESTING.md) for patterns and fixtures.

**Import-linter enforces architecture.** Control cannot import execution. Ports cannot import outer layers. If your change violates a boundary, `make validate` will catch it.

## Key workflows to understand

| Topic | Doc | When you need it |
|-------|-----|------------------|
| How agents complete work | `coding_done.py`/`reviewer_done.py` + CLAUDE.md "Agent Intent, Orchestrator Authority" | Modifying completion processing |
| Code review loop | [Review Workflow](../development/REVIEW_WORKFLOW.md) | Modifying review, rework, or triage |
| Hook enforcement | [Hooks Architecture](../architecture/hooks.md) | Modifying safety guardrails |
| State machines | `domain/state_machines/` | Changing issue or review lifecycle |
| Events and observability | CLAUDE.md "Events vs Logs" | Adding new observable behavior |

## Submitting changes

1. Run `make validate` before pushing — this is what CI runs
2. Tests must pass. If tests fail, fix them — don't defer.
3. [CONTRIBUTING.md](../../CONTRIBUTING.md) covers running tests from forks

## Development docs reference

These docs in `docs/development/` cover specific topics in depth:

| Doc | When to read |
|-----|--------------|
| [Testing Guide](../development/TESTING.md) | Test patterns, fixtures, mocking |
| [Troubleshooting](../development/TROUBLESHOOTING.md) | Debugging sessions, hooks, common issues |
| [Review Workflow](../development/REVIEW_WORKFLOW.md) | Code review pipeline, exchange mechanisms |
| [Debugging](../development/DEBUGGING.md) | Event system debugging |
| [Caching & ETags](../development/CACHING_ETAGS.md) | GitHub API caching implementation |
| [GitHub Token Setup (Dev)](../development/GITHUB_TOKEN_SETUP.md) | Token resolution chain internals |
