# ADR 0003: Model inbound truth as Observations (not mixed facts/decisions)

**Status:** Accepted  
**Date:** 2025-12-31

## Context
The orchestrator needs to ingest information from the external world:
- GitHub snapshots (issues, labels, PRs)
- Terminal/session status (running/exited)
- Files/worktrees (completion artifacts)
- WebUI/test harness signals (optional)

If these inbound inputs mix *facts* with *policy interpretation* (e.g., “COMPLETED”), boundary clarity degrades:
- adapters begin to encode policy
- domain/control logic becomes inconsistent
- testing becomes harder (because semantics are distributed)

## Decision
Use a single conceptual model for inbound inputs: **Observations**.

An **Observation** is:
- a report of what was seen, where it was seen, and when
- optionally accompanied by metadata for ordering/troubleshooting (cursor, etag, monotonic event_id)
- explicitly *not* a policy decision

Adapters produce Observations. Control-plane code consumes Observations and produces Plans/Actions.

We do not maintain separate “fact vs observation” concepts. In this system, **facts are observations**.

## Consequences
### Positive
- Clean separation: adapters observe; planner decides; applier executes.
- Easier mocks: tests synthesize Observations directly.
- Better auditability: observations can be logged, stored, replayed.

### Negative / Costs
- Requires discipline to avoid reintroducing implicit policy in adapters.

## Alternatives considered
- Keep derived statuses in adapters (e.g., SessionStatus.COMPLETED): rejected as it embeds policy.
- Pure event sourcing: not needed yet; observations are sufficient.

## Follow-ups
- Introduce `ObservationEnvelope` with:
  - `event_id` (monotonic per source stream)
  - `cursor` / `etag` (where applicable)
  - `observed_at`
  - `source` (github/terminal/fs)
- Ensure the planner only depends on observations, not adapter internals.
