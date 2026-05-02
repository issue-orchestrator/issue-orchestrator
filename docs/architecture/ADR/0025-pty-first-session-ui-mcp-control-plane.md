# ADR 0025: PTY-first session UI with MCP as control plane

**Status:** Accepted
**Date:** 2026-05-02

## Context

The orchestrator runs coding and review agents as interactive sessions. The current direction is:

- one live session for coding and rework
- one live session for review
- users inspect and interact with those sessions through the PTY-backed UI

MCP is also a possible interface for the orchestrator. It can expose tools and resources to AI clients, making orchestrator state and operations easier to consume programmatically.

The key product requirement is that users must be able to see the agent UI as they would normally see it. That includes prompts, terminal state, model output, command output, auth failures, stalls, completion commands, and other live interaction details that are only visible in the actual session surface.

## Decision

**Use PTY-backed session UI as the canonical live user surface. Use MCP as a structured integration and control plane, not as the replacement UI for live agent work.**

Live coding, rework, and review work should remain visible through the same session UI a user would normally inspect. The orchestrator may expose MCP tools and resources for structured access, but MCP must not become the source of truth for what the live agent UI looked like.

MCP is appropriate for:

- listing issues, sessions, reviews, and current lifecycle state
- reading structured events, artifacts, completion records, and validation results
- requesting orchestrator-owned actions such as start, stop, pause, retry, or open session
- returning links or identifiers for the live PTY/session viewer
- integrating external AI clients without requiring them to parse terminal output

MCP is not appropriate for:

- replacing the PTY/session viewer as the canonical live user experience
- hiding prompts, terminal state, or agent interaction details behind summarized tool output
- bypassing completion records, hooks, validation, labels, or orchestrator-owned state transitions
- making MCP client rendering the compatibility target for the user-facing UI
- parsing terminal output as a policy API

The UI contract is therefore layered:

1. PTY/session viewer: canonical live human-visible surface.
2. Events and completion records: canonical structured system facts.
3. MCP: optional external integration surface over orchestrator-owned facts and actions.

## Consequences

### Positive

- Users see the actual session surface, including failures and interactive prompts.
- Debugging remains grounded in the raw session record rather than a client-specific abstraction.
- Coding and review responsibilities stay separated through distinct sessions.
- MCP can provide clean structured automation without turning terminal output into an API.
- The orchestrator remains the authority over labels, validation, PR actions, and lifecycle transitions.

### Negative

- PTY lifecycle management remains a real system responsibility: timeouts, cleanup, replay, focus, input routing, and stalled prompts must be handled explicitly.
- The UI needs tests that exercise the actual session viewer behavior, not only structured API responses.
- External clients may still need to open the session UI for full fidelity instead of relying only on MCP summaries.
- Maintaining both a live UI surface and a structured MCP surface adds contract discipline.

### Risks

- MCP tools could drift into a parallel product surface if they duplicate UI workflows instead of linking to the canonical session view.
- Summaries can hide important agent behavior if they are treated as substitutes for the live session record.
- Low-level MCP operations could bypass orchestrator policy if they are not designed around owner abstractions.

## Alternatives Considered

1. **MCP as the primary UI.** Rejected. MCP client rendering is not the normal user UI, varies by client, and cannot faithfully show the live terminal/session experience.

2. **PTY-only with no MCP surface.** Rejected. PTY is high fidelity for humans but poor as a structured machine interface. External tools should not parse terminal output for state.

3. **Dual equal surfaces.** Rejected. Treating PTY UI and MCP as equivalent user surfaces invites drift and unclear support obligations.

## Implementation Guidance

- Keep live session display and interaction in the PTY/session viewer path.
- Expose MCP resources and tools at behavior-level boundaries owned by the orchestrator.
- Prefer structured events, completion records, and existing owner abstractions over terminal parsing.
- When an MCP client needs full-fidelity inspection, return a link or session identifier for the canonical session UI.
- Test MCP behavior as structured integration behavior, and test the session UI as the user-facing live experience.

## Related

- ADR-0016: Orchestrator as mediator
- ADR-0019: Structured completion protocol
- ADR-0023: Deterministic orchestrator required
- `docs/architecture/control_center_lifecycle_model.md`
