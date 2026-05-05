# Architectural Decision Records (ADRs)

These ADRs capture the *few* architectural decisions that materially affect correctness, security, boundaries, and extensibility.

**Rules**
- ADRs are append-only: do **not** rewrite history. If a decision changes, add a new ADR that **supersedes** an older one.
- Keep ADRs short (aim for ~1 page).
- Prefer decisions that prevent architectural drift over “nice-to-have” notes.

## Index

- [0001 Use a single GitHub HTTP client (httpx sync) and avoid gh/ghapi in runtime](0001-single-github-http-client-httpx-sync.md)
- [0002 Treat writes as untrusted until observed (write → verify loop)](0002-write-then-observe.md)
- [0003 Model inbound truth as Observations (not mixed facts/decisions)](0003-observations-as-inbound-truth.md)
- [0004 Centralize reconciliation (startup + runtime) behind a single entrypoint](0004-centralize-reconciliation.md)
- [0005 Enforce human merge and agent credential isolation](0005-human-merge-and-agent-credential-isolation.md)
- [0006 Cache external reads with explicit refresh policy + ETags](0006-caching-and-refresh-policy.md)
- [0007 Verify external state before mutation (optimistic concurrency)](0007-external-state-reconciliation.md)
- [0008 GitHub auth sources](0008-github-auth-sources.md)
- [0009 Dependency scoping](0009-dependency-scoping.md)
- [0010 Verify failure policy](0010-verify-failure-policy.md)
- [0011 Hexagonal architecture (ports and adapters)](0011-hexagonal-architecture.md)
- [0012 Mechanical guardrails over policy documents](0012-mechanical-guardrails.md)
- [0013 GitHub labels as crash-safe source of truth](0013-labels-as-crash-safe-truth.md)
- [0014 Observer → Planner → ActionApplier loop pattern](0014-observe-plan-apply-loop.md)
- [0015 Log enough that problem resolution is trivial](0015-log-for-trivial-debugging.md)
- [0016 Orchestrator as mediator (agents never touch GitHub)](0016-orchestrator-as-mediator.md)
- [0017 Orchestrator coordinates, Planner decides](0017-orchestrator-coordinates-planner-decides.md)
- [0018 Git worktree isolation per agent session](0018-worktree-isolation.md)
- [0019 Structured completion protocol (agent-done)](0019-agent-done-completion-protocol.md)
- [0020 Single validation command (e2e excluded for speed)](0020-single-validation-command.md)
- [0021 Optional Docker-based agent isolation](0021-docker-isolation.md)
- [0022 Issue-specific base branch (default main)](0022-ISSUE-BASE-BRANCH.md)
- [0023 Deterministic orchestrator required (cannot replace with agentic-first solution)](0023-deterministic-orchestrator-required.md)
- [0024 Test-driven development workflow for agents](0024-tdd-workflow-for-agents.md)
- [0025 PTY-first session UI with MCP as control plane](0025-pty-first-session-ui-mcp-control-plane.md)
- [0026 Issue-lifecycle persistent coder/reviewer pair](0026-issue-lifecycle-persistent-exchange-pair.md)
