# Issue-Orchestrator: Making Agentic Development Sustainable

This essay is the design backstory behind Issue-Orchestrator's enforcement model. For the concise product thesis, read [No Free Lunch for Coding Agents](../journeys/no-free-lunch.md).

The short version: AI agents are good at finishing bounded tasks, but they are not automatically good at maintaining systems. Task completion without enforced architecture, validation, and review slowly erodes a codebase.

I wanted to be able to give my agentic helpers a list of tasks and still keep the system well-architected, validated, and reviewable over time. Prompting and exhortation were not enough. The design lesson was enforcement.

---

## Three Tracks for Sustainable Agentic Development

After a lot of trial and error, I found that three things had to exist simultaneously:

1. A named, enforceable architecture
2. A deterministic workflow
3. Externalized task management

If any one of those is missing, the system degrades.

---

## Track 1: Architecture Must Be Named — and Enforced

If you can't name your architecture, you don't have one.

In my case, I chose a hexagonal architecture. That decision wasn't about fashion; it was about enforceable boundaries. I structured the repository so that dependencies were obvious and then created guardrails to make violations impossible.

In Python, that meant:

- import-linter rules to prevent forbidden dependencies
- custom AST checks for constraints linters can't express
- validation gates that run on demand, pre-push, and in CI

These gates cannot be bypassed. AI-level hooks block `--no-verify` before it executes. Git hooks run validation pre-push. The orchestrator independently requires a passing validation record before advancing state. CI re-validates in a clean environment. If a change violates a boundary, it doesn't move forward.

One of the most important lessons I learned was this: agents will always find reasons to skip validation unless it is technically impossible to do so. The only durable solution is to make skipping impossible.

---

## Track 2: Workflow Must Be Deterministic

Architecture protects structure. Workflow protects quality.

The workflow I settled on is simple and opinionated:

1. Define small, human-reviewable tasks.
2. Assign each task to a specific agent type (web, backend, etc.).
3. Require validation — including guardrails and tests — before any progress.
4. Run a structured code review agent.
5. If review fails, cycle back a bounded number of times.
6. Leave a draft PR for human approval.
7. Periodically run a triage agent to examine failures and patterns.

The important part is not the steps. It's that the steps are enforced.

Agents do not decide that their work is "good enough." The orchestrator decides based on explicit outcomes.

---

## Track 3: Externalize Management

At some point I realized I was reinventing task management badly. So I leaned into GitHub issues and milestones.

GitHub provides:

- a durable, shared source of intent
- simple dependency modeling
- visibility for humans

On top of that, I built Issue-Orchestrator.

Issue-Orchestrator claims GitHub issues, runs agents in isolated worktrees with minimal permissions, enforces validation and review gates, and moves state forward only when constraints are satisfied. Humans approve PRs.

Agents execute bounded work.
The orchestrator enforces process.

---

## What Issue-Orchestrator Actually Does

The system is designed as a headless control plane.

It:

- Claims issues and assigns them to appropriate agent types.
- Runs agents concurrently in isolated worktrees.
- Prevents agents from pushing directly or bypassing validation.
- Interprets structured completion output from agents.
- Enforces validation and review cycles deterministically.
- Preserves in-flight work across crashes or restarts.
- Reconciles its internal state with GitHub before mutating external state.
- Surfaces logs, timelines, and structured events for observability.

The UI — whether local web or IDE-integrated — is just a client. The orchestration core is decoupled from presentation.

---

## Real-World Complexity: GitHub Is Not a Database

This was harder than I expected.

GitHub is eventually consistent. Humans can change labels at any time. Processes crash. Main branches move. Provider APIs fail.

A significant portion of the system exists to reconcile a deterministic workflow against an unreliable, human-mutable coordination substrate.

To handle this, the orchestrator:

- double-checks expected state before updates
- uses idempotent operations wherever possible
- performs reconciliation at startup and before mutation
- classifies failures (transient vs fatal)
- implements short retries with jitter
- opens circuit breakers for longer outages
- schedules controlled retries instead of thrashing

Failure is treated as normal, not exceptional.

---

## Concurrency Without Chaos

To maximize throughput, I allow multiple agents to work concurrently. But concurrency is bounded and explicit.

Each issue:

- lives in its own worktree
- has a defined state machine
- moves through deterministic transitions

No implicit coordination. No silent state mutation.

Dirty trees can be disallowed. Validation must pass before moving forward. Review loops are bounded. Agents may declare failure, but they do not declare success unilaterally.

---

## Dogfooding and Promotion

One unexpected effect of building this system is that it changed my role.

Instead of working directly on code, I now:

- define milestones
- identify critical user journeys
- break them into right-sized issues
- let agents decompose and execute under constraint
- review PRs and intervene when something is genuinely ambiguous

In some sense, I was promoted.

Agents are happiest when given bounded, well-scoped tasks. Complex, multi-step instructions rarely work as intended. Breaking work into human-scale reviewable parts has been far more effective than trying to coerce agents into handling large, loosely defined objectives.

---

## Experimental Layers

Work has begun on higher-level goal specification — operating at the "critical user journey" layer. The idea is to let agents help generate and sequence work at a higher abstraction level, while still relying on the lower-level orchestrator to enforce execution constraints.

This layer is explicitly experimental and advisory. Execution remains strictly constrained.

---

## Relationship to the Core Thesis

This essay is background, not the canonical product thesis. [No Free Lunch for Coding Agents](../journeys/no-free-lunch.md) states the public contract: agents contribute work, while the system decides whether that work advances.

This document preserves the design path that led there: enforce architecture mechanically, make workflow deterministic, externalize task management, and leave humans with merge authority.
