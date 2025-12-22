# Issue Orchestrator

**A conservative, guardrailed system for using AI agents on real code without losing control.**

Issue Orchestrator lets AI agents work on GitHub issues and codebases while enforcing mechanical guardrails: tests must pass, reviews must happen, and agents cannot silently advance state or merge code. The system is designed to make agent behavior *predictable, auditable, and stoppable*.

This is not an autonomy-first framework. It is a **control-first orchestration system**.

---

## Why this exists

Modern LLM agents are capable of producing large, coherent code changes — but they are not reliable custodians of process.

In practice, prompts alone are not sufficient to enforce:
- running the *right* tests (or all tests),
- respecting review cycles,
- keeping workflow state consistent,
- or stopping safely when things go wrong.

Issue Orchestrator was built after repeatedly discovering that even very strong agents need **mechanical guardrails**, not just instructions.

---

## Core principles

### 1. Agents are contributors, not authorities
Agents may:
- write code,
- propose changes,
- explain intent.

Agents may **not**:
- merge code,
- advance workflow state unilaterally,
- bypass reviews or tests.

Those responsibilities live in the control plane.

---

### 2. Process is enforced mechanically
Key invariants are enforced by the system, not by agent compliance:

- Tests must pass before publishing work
- Reviews always occur (agent or human)
- State transitions are validated
- Failures stop the system rather than guessing

If an invariant is violated, the system escalates instead of continuing.

---

### 3. GitHub is the shared control surface
GitHub issues, labels, and pull requests are used as:
- the visible source of truth,
- the collaboration interface,
- the audit trail.

Local artifacts (worktrees, tmux sessions, intermediate files) are treated as *caches*, not as canonical state.

---

### 4. Separation of control and execution
The system is explicitly split into:
- a **control plane** that decides *what should happen*,
- an **execution plane** that performs work (agents, shells, sessions).

Agents emit **structured intent**, not side effects.
The orchestrator decides whether and how to publish results.

---

## What the system does

At a high level:

1. GitHub issues represent units of work
2. The orchestrator claims eligible issues
3. An agent performs work in an isolated worktree/session
4. A review agent evaluates the result (bounded retries)
5. Tests are run and enforced
6. The orchestrator publishes results (PRs, labels, comments)
7. State is reconciled against GitHub and advanced safely

At every step, failure is explicit and visible.

---

## What this is *not*

This system is **not**:
- an agent autonomy framework,
- a prompt engineering playground,
- a one-shot task runner,
- a "merge on green" bot,
- a demo-first tool.

It is intentionally conservative.

If your goal is maximum speed or minimal friction, this is probably not for you.

---

## Guardrails (brief)

Some examples of enforced guardrails:

- Agents cannot push or merge directly
- Publishing requires passing tests
- Reviews are mandatory and bounded
- Workflow state must reconcile with GitHub
- Conflicting or stale state halts progress

For implementation details, see [`HOOKS.md`](./HOOKS.md).

---

## Architecture (high level)

- **Core**
  - Explicit state machines (issue, review, session)
  - Planning logic (decide next actions)
  - Reconciliation logic (detect drift/anomalies)

- **Adapters**
  - GitHub
  - Git (worktrees)
  - tmux / terminal runners

- **Execution**
  - Agent sessions
  - Review sessions
  - Structured completion protocol

- **Events**
  - A single event emitter publishes trace events
  - Web UI and telemetry subscribe passively

The core does not depend on pluggy, GitHub SDKs, or terminals.

For details, see [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md).

---

## Architecture Guardrails

This repo enforces import boundaries with **import-linter** to prevent architecture drift.

```bash
# Run locally
pip install import-linter
lint-imports
```

See [`.github/workflows/architecture-guardrails.yml`](.github/workflows/architecture-guardrails.yml) for CI enforcement.

---

## Status

This project is actively used on a real side project and is being prepared for public release as an **open-core reference implementation**.

The goal is not maximal feature coverage, but to demonstrate a disciplined approach to agent orchestration that can scale from solo developers to small teams.

---

## Why this is public

This repository is intended as a **design artifact** as much as a tool.

It reflects hard-won lessons about:
- agent failure modes,
- state management,
- reconciliation in eventually consistent systems,
- and the limits of prompt-based control.

---

## Who should read this code

This project will be most interesting to:
- senior engineers thinking about agentic systems,
- people designing control planes and orchestration layers,
- anyone skeptical of "just let the agent do it."

---

## License

MIT
