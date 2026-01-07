# ADR: Optional Docker-based agent isolation

**Status:** Accepted  
**Date:** 2026-01-XX

## Context
Issue-orchestrator executes AI agents locally to perform coding, review, and diagnosis tasks.

On macOS, it is not possible to fully sandbox an untrusted local process:
- PATH hardening does not prevent absolute-path execution (e.g. `/usr/bin/security`)
- macOS Keychain access cannot be reliably restricted per process
- OS-level sandboxing is not practical for arbitrary CLI tools

Therefore, local agent execution can only provide **best-effort guardrails**, not a strong security boundary.

## Decision
Issue-orchestrator will support **two explicit agent execution modes**:

1. **Local mode (default)**
    - Best-effort isolation
    - PATH and environment stripping
    - No credentials intentionally passed to agents
    - Designed to prevent accidental misuse, not adversarial escape

2. **Docker mode (optional)**
    - Agents run inside a Docker container
    - No access to host Keychain or system binaries
    - Only the worktree and orchestrator state directories are mounted
    - No secrets are mounted unless explicitly configured
    - Provides a strong, OS-enforced isolation boundary

Local mode remains the default for ergonomics and fast iteration.
Docker mode is recommended for production, CI, or untrusted agent workflows.

## Consequences
### Positive
- Honest and accurate security posture
- Strong isolation available when needed
- Clear explanation for reviewers and users
- Minimal impact on existing workflows

### Negative
- Additional complexity to support Docker runner
- Slower execution compared to local mode

## Follow-ups
- Implement `docker` agent runner using existing command-runner abstraction
- Provide `Dockerfile.agent` and documentation
- Expose execution mode in `.issue-orchestrator/config/` and setup wizard