# ADR 0024: Test-Driven Development workflow for agents

**Status:** Accepted
**Date:** 2025-01-20

## Context

Anthropic recommends test-driven development (TDD) as a "favorite workflow" for agentic coding:

> "Test-driven development becomes even more powerful with agentic coding: Ask Claude to write tests based on expected input/output pairs... Claude performs best when it has a clear target to iterate against."
> — [Claude Code: Best practices for agentic coding](https://www.anthropic.com/engineering/claude-code-best-practices)

The rationale:
- Tests give agents a **verifiable target** to iterate against
- **Reduced drift** - less likely to introduce unintended side effects
- **Natural decomposition** - complex problems break into testable units
- Tests written before implementation avoid the "test what I built" trap

Additionally, Anthropic suggests a multi-agent approach (one writes tests, another implements) can yield better results than single-agent doing both. However, this adds orchestration complexity.

Our current prompts have tests as step 5 (after implementation) - implementation-first, not TDD.

## Decision

### 1. Encourage TDD in coding prompts (where appropriate)

Update coding agent prompts to follow a test-first workflow for suitable tasks:

```
Phase 1: Tests First (RED)
- Write tests for the feature
- Verify tests fail (confirms they test the right thing)
- Commit tests

Phase 2: Implementation (GREEN)
- Write minimum code to make tests pass
- Do NOT modify tests to make them pass

Phase 3: Polish (REFACTOR)
- Refactor while keeping tests green
```

**Not all tasks are TDD-suitable.** Prompts should guide agents to choose:

| Task Type | Approach |
|-----------|----------|
| New feature | Full TDD - tests first |
| Bug fix (known behavior) | Write failing test that reproduces bug, then fix |
| Bug fix (unknown cause) | Investigate first, then write regression test |
| Refactoring | Ensure existing tests pass; add characterization tests if needed |
| Exploration/spike | Implement first, tests after (if kept) |

### 2. Add behavioral test review to code review criteria

Reviewers should verify tests follow our testing principles (`tests/AGENTS.md`):

- Tests verify WHAT code does, not HOW (behavioral, not implementation-coupled)
- No access to private members (`_xxx`)
- Tests exercise public API, not internal helpers
- Litmus test: "Would a user of this code care about this?"

This catches implementation-coupled tests that will break on refactoring.

### 3. Defer multi-agent TDD

A separate test-writing agent is theoretically purer (tests without implementation bias) but adds orchestration complexity. Defer this until we see how single-agent TDD performs.

Future consideration:
```
Test Agent (writes tests) → Coding Agent (implements) → Review Agent
```

## Consequences

### Positive
- Better test quality from test-first thinking
- Tests actually test behavior, not implementation details
- Agents have clearer targets to work toward
- Reviewers catch implementation-coupled tests
- Aligned with Anthropic's recommended practices

### Negative
- Agents may struggle with TDD for investigative tasks
- Slight increase in prompt complexity
- Review criteria expansion adds reviewer burden

### Risks
- Agents might write tests that pass trivially
- TDD may slow down exploratory work
- Need monitoring to see if TDD actually improves outcomes

## Alternatives Considered

1. **Full multi-agent TDD** - One agent writes tests, another implements. Purer but complex orchestration. Deferred.

2. **No change** - Keep implementation-first. Rejected - Anthropic's research shows TDD improves agent performance.

3. **Mandatory TDD for all tasks** - Too rigid. Some tasks (investigation, spikes) don't fit TDD.

## Related

- `tests/AGENTS.md` - Behavioral testing principles
- `examples/prompts/feature.md` - Updated with TDD guidance
- `examples/prompts/code-review.md` - Updated with test review criteria
- [Anthropic: Claude Code best practices](https://www.anthropic.com/engineering/claude-code-best-practices)
