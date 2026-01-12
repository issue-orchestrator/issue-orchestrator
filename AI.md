# AI Assistant Guide for issue-orchestrator

## What This Is

**issue-orchestrator** orchestrates multiple Claude Code agents working on GitHub issues in parallel. It fetches issues, creates git worktrees, launches agent sessions (tmux/iTerm2/web), enforces structured completion via `agent-done`, and manages the full lifecycle including code review and triage.

## Architecture Principles

1. **Hexagonal (Ports & Adapters)** - Core defines Protocol interfaces (ports), adapters implement them. External systems are abstracted behind ports.

2. **Dependency Injection** - Orchestrator receives dependencies via constructor, not globals. `bootstrap.py` is the composition root that wires everything.

3. **Layered Separation**:
   - **Observation** (`observation/`) - Gathers facts, no decisions
   - **Control** (`control/`, `orchestrator.py`) - Makes decisions, advances state
   - **Execution** (`execution/`) - Talks to external systems, no policy

4. **Labels as Source of Truth** - Crash-safe: GitHub labels persist, orchestrator recovers state from labels on restart.

## Key Ports (Protocols)

| Port | Purpose | Adapter |
|------|---------|---------|
| `EventSink` | Fire-and-forget trace events | `PluggyEventSink` |
| `SessionRunner` | Terminal session management | `PluggySessionRunner` |
| `IssueTracker` | GitHub issue operations | `GitHubAdapter` |
| `SessionStore` | Persist session state | `JsonSessionStore` |

## Composition Root

```python
# bootstrap.py - the ONLY place that wires dependencies
from .bootstrap import build_orchestrator

orchestrator = build_orchestrator(config)  # Production
orchestrator = build_orchestrator_for_testing(config, events=mock, runner=mock)  # Tests
```

## Core Files

| File | Purpose |
|------|---------|
| `bootstrap.py` | Composition root - wires all dependencies |
| `orchestrator.py` | Main loop, session lifecycle, delegates to ports |
| `cli.py` | CLI commands, calls `build_orchestrator()` |
| `agent_done.py` | `agent-done` CLI - enforced completion command |
| `ports/` | Protocol definitions (EventSink, SessionRunner, etc.) |
| `execution/` | Adapters (GitHub, terminal plugins, stores) |

## The `agent-done` Command

Agents MUST use `agent-done` to complete. Direct `git push` is blocked by hooks.

```bash
agent-done completed --implementation "What was done" --problems "None"
agent-done blocked --reason "Why" --attempted "What was tried"
agent-done approved --summary "Code looks good"  # Reviewer
agent-done changes_requested --issues "Missing tests"  # Reviewer
```

## Detailed Documentation

| Topic | File | When to Read |
|-------|------|--------------|
| Architecture deep-dive | [docs/architecture/README.md](docs/architecture/README.md) | System design, DI patterns, adding adapters |
| Review workflow | [docs/development/REVIEW_WORKFLOW.md](docs/development/REVIEW_WORKFLOW.md) | Code review, triage, rework cycles |
| Troubleshooting | [docs/development/TROUBLESHOOTING.md](docs/development/TROUBLESHOOTING.md) | Debugging, common problems |
| Hook enforcement | [docs/architecture/hooks.md](docs/architecture/hooks.md) | Guardrails, agent restrictions |

## Skills (Auto-Invoked)

Skills in `.claude/skills/` are automatically invoked when working on relevant areas:

| Skill | Triggers When |
|-------|---------------|
| `architecture` | Working on ports, adapters, DI, bootstrap |
| `troubleshooting` | Debugging sessions, hooks, locks |
| `review-workflow` | Code review pipeline, triage, rework |

## Directory Context (CLAUDE.md)

Test directories have local `CLAUDE.md` files auto-read when working there:
- `tests/unit/CLAUDE.md` - Unit test patterns, fixtures, mocking
- `tests/e2e/CLAUDE.md` - E2E setup, gh auth, test-data isolation
- `tests/integration/CLAUDE.md` - Integration test patterns

## Quick Reference

**Run tests:**
```bash
pytest tests/unit/ -v          # 1100+ unit tests
pytest tests/e2e/ -v           # Live e2e tests (requires gh auth)
```

## Async E2E Runner

The orchestrator includes an async E2E test runner. See [docs/user/e2e.md](docs/user/e2e.md) for details.

**Configuration:** `.issue-orchestrator/config/<name>.yaml` - see `examples/config.example.yaml`

## Events vs Logs (Key Patterns)

**Events** (machine-consumable):
- Audience: Web UI, tests, automations
- Stable names from `EventName` enum (`events/catalog.py`)
- Structured payloads with `run_id`, `tick_id`, `schema` version
- Emit via `self.events.publish(TraceEvent(EventName.TICK_STARTED, ctx.enrich({...})))`

**Logs** (human-consumable):
- Audience: Developers reading console/CI logs
- Use `logging.getLogger(__name__)` with levels DEBUG/INFO/WARNING/ERROR
- Include context via `extra=log_context(tick_id=1, issue_key="M1-011")`
- Can change freely - UI/tests must NOT parse logs

**Golden Rule**: UI and tests react to events, never parse log text.

```python
# Events - for UI/test synchronization
from .events import EventName, EventContext
self.events.publish(TraceEvent(EventName.TICK_COMPLETED, ctx.enrich({"idle": True})))

# Logs - for debugging
logger.info("[PLAN] %d action(s)", count, extra=log_context(tick_id=5))
```

## Conventions

- Ports in `ports/`, adapters in `execution/`
- Tests mock at port boundaries, not internal functions
- Events via `self.events.publish(TraceEvent(...))`, never direct pluggy
- Session ops via `self.runner.*`, never direct plugin manager
- All orchestrator dependencies injected via constructor

## GitHub API Discipline

GitHub CLI/API calls are a limited resource. Be mindful of command volume and avoid unnecessary scans or polling. Inefficient usage forces expensive systemic tuning later, so prefer cached data, targeted reads, and minimal refreshes whenever possible.
Direct `gh` CLI usage from Python is forbidden; token resolution must use explicit config/env or OS keychain/hosts.yml.
