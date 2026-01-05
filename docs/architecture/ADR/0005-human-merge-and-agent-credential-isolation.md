# ADR 0005: Enforce human merge and agent credential isolation

**Status:** Accepted  
**Date:** 2025-12-31

## Context
Agents are untrusted: they can and will attempt shortcuts (e.g., `--no-verify`, direct merges, bypassing guardrails). The system’s value proposition includes guardrails that enforce invariants:
- tests run and pass before pushing/PR readiness
- labels/state transitions remain consistent
- humans are required for merge (final gate)

Relying on “agent good behavior” is insufficient. Credential inheritance is a key risk (shell environment, git credential helpers, local GH auth).

## Decision
1. **Humans merge**:
   - The orchestrator and agents must not have credentials that can merge.
   - Enforce via GitHub branch protections + token scope reduction where possible.
   - PRs remain Draft until review passes; humans transition/merge.

2. **Agent credential isolation**:
   - Agents operate without GitHub API credentials and without `gh` CLI merge capabilities.
   - The orchestrator is responsible for GitHub writes using a constrained token.
   - Use affirmative tests (per worktree/session) to verify no credential leakage.

3. **Guardrail enforcement**:
   - Block dangerous commands (e.g., `--no-verify`) via hooks where possible.
   - Add static guardrails (AST checks / import-linter) to prevent reintroducing unsafe primitives in core.

## Consequences
### Positive
- System invariants survive even adversarial agent behavior.
- Clear security story (staff-level signal).
- Reduced chance of “silent bad merges.”

### Negative / Costs
- Additional setup complexity (token scopes, branch protections).
- Requires careful orchestration of “agent runs locally, orchestrator pushes” or equivalent patterns.

## Alternatives considered
- Let agents use the developer’s credentials: rejected (agents can merge / bypass).
- Let orchestrator merge automatically: rejected (violates “humans merge” invariant).
- Fully server-side execution (GitHub Actions only): future option; not required for local-first flows.

## Implementation: Isolation Modes

### Standard Mode (default, no sudo)
- Agents run as the current OS user
- Orchestrator launches agent sessions with a scrubbed environment and an isolated HOME per worktree
- A fast **affirmative sandbox verification** runs at worktree/session start

### Hardened Mode (opt-in, sudo once)
- Agents run under a dedicated low-privilege OS user with no credentials
- Prevents GitHub API access by construction

### Sandbox Verification (both modes)

Minimum checks before agent session starts:
- `gh auth status` must fail
- `git push --dry-run` must fail fast (no prompt)
- Forbidden env vars absent (`GITHUB_TOKEN`, `GH_TOKEN`, `SSH_AUTH_SOCK`, etc.)
- Mode-specific: HOME isolated (standard) or `whoami` is sandbox user (hardened)

On verification failure:
- Refuse to start agent session
- Emit trace event `sandbox_verification_failed`

## Related ADRs
- ADR-0002: Write-then-observe (verify orchestration writes)
