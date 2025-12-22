**Audience:** Design document (public). Not a usage guide.

# Core Concepts

Issue Orchestrator is built around three ideas:

1) **Agents do work locally.**
2) **The orchestrator is the control plane.**
3) **Guardrails are mechanical.**

These constraints are reinforced by:
- sandbox verification per worktree/session
- validation as a publish gate with suite+SHA caching
- import boundaries to keep core testable
