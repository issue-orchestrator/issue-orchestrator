# ADR 0023: Deterministic orchestrator required (cannot replace with agentic-first solution)

**Status:** Accepted
**Date:** 2025-01-16

## Context

The issue-orchestrator manages multiple Claude Code agents working on GitHub issues in parallel. As AI capabilities improve, a natural question arises: can the orchestrator itself be replaced with an LLM-driven "agentic-first" solution?

This ADR evaluates whether a well-crafted prompt (or set of prompts) could replace the deterministic Python orchestrator with Claude Code acting as both the orchestrator and the worker.

## Options Considered

### Option A: Pure Prompt Orchestration

Claude Code receives instructions like:
- "Check GitHub for issues labeled agent:backend"
- "Create worktrees, spawn Task agents to work on issues"
- "When done, create PR, update labels"

**Problems:**
- Non-determinism: Same input → different outputs
- Hallucination risk: LLM might invent labels or misremember state
- No persistent memory: Session state lost on restart
- Untestable: Cannot unit test "will the LLM make the right choice"
- Cost/latency: Every decision requires API call

### Option B: Shell Script + Claude Code Workers

Minimal shell script manages parallelism; Claude Code does actual work.

**Problems:**
- Must rebuild edge case handling (orphaned in-progress issues, retries, timeouts)
- No code review pipeline without significant additional code
- Once edge cases are handled, approaches Python orchestrator complexity

### Option C: Keep Deterministic Orchestrator

Current architecture: Python orchestrator as control plane, Claude Code agents as untrusted workers.

## Decision

**Keep the deterministic orchestrator. An agentic-first solution cannot reliably replace it.**

### The Trust Boundary Problem

The orchestrator exists as a **trust boundary** between untrusted LLM agents and trusted external systems (GitHub):

```
UNTRUSTED: Agent (LLM)     →  completion.json  →  TRUSTED: Orchestrator (code)
   can hallucinate              validated            applies labels, creates PRs
   can try to bypass                                 enforces state machines
   has no credentials                                has GitHub token
```

If the orchestrator itself is an LLM, there is no trust boundary. You would need a meta-orchestrator to validate the orchestrator's decisions—turtles all the way down.

### What Requires Deterministic Control

| Capability | Why LLM Cannot Replace |
|------------|------------------------|
| State transitions | Must be deterministic or issues get stuck |
| Label application | Labels are source of truth—hallucination = corruption |
| Session lifecycle | Timeouts must be mechanical, not "LLM judgment" |
| Credential isolation | Code enforces agents have no GitHub access |
| Guardrails | Prompts can be jailbroken, hooks cannot |
| Crash recovery | Read labels on startup—LLM has no persistent memory |
| Code review pipeline | Rework cycles require reliable state tracking |

### What Could Be LLM-Driven (Bounded Points)

| Decision | Safe? | Reason |
|----------|-------|--------|
| Issue prioritization | Yes | Wrong choice = suboptimal, not broken |
| Retry vs escalate | Yes | Heuristic judgment, recoverable |
| PR description text | Yes | Cosmetic |
| Tech Lead batching | Yes | Timing optimization |

## Consequences

### Positive

- Reliability: Deterministic state machines don't hallucinate
- Debuggability: Code has stack traces, LLM decisions don't
- Testability: Can unit test orchestrator logic
- Security: Mechanical guardrails survive adversarial agents
- Crash recovery: Labels persist, orchestrator restarts cleanly

### Negative

- Maintenance burden: ~15k lines of Python to maintain
- Rigidity: Edge cases require explicit code, not LLM flexibility
- Complexity: Full-featured orchestrator is non-trivial

### Mitigation

The orchestrator can be **simplified** while keeping deterministic control:
- Remove pluggy plugin system (premature extensibility)
- Simplify state machines (transitions library adds ceremony)
- Remove dead code (unimplemented adapters)
- Inject LLM decisions at bounded, non-critical points

## Related

- ADR-0012: Mechanical guardrails
- ADR-0016: Orchestrator as mediator
- ADR-0019: Agent-done completion protocol
