# Control Center Lifecycle Model

## Purpose

Define one stable mental model for the Control Center UI and repository runtime lifecycle.

## Scope Model

1. **Control Center**: the local UI shell and dashboard process.
2. **Repository Engine**: per-repository runtime that orchestrates work.
3. **Jobs**: issue-level execution units managed by a repository engine.

The Control Center is a client surface. Repository engines are the long-lived runtime entities.

## Core Behavior

1. Closing Control Center does **not** stop repository engines.
2. Engine lifecycle is controlled at engine scope: `Start engine`, `Pause engine`, `Resume engine`, `Stop engine`.
3. Global controls are only app-level or bulk actions.
4. Left navigation remains a stable set of view selectors and does not add/remove entries based on runtime state.

## UI Placement Rules

1. Global header contains app-level actions and aggregate status only.
2. Per-engine controls live on engine cards and engine detail view.
3. Per-engine config selection lives on engine surfaces (overview card and/or detail), not global header.
4. Destructive action labels must be explicit:
   - `Stop engine` for single engine scope.
   - `Stop all engines` for bulk scope.
   - Avoid ambiguous `Shutdown` in engine contexts.

## State Vocabulary

Use this exact set everywhere in Control Center:

- `Running`
- `Paused`
- `Not running`

Use this exact set for engine actions:

- `Start engine`
- `Start paused`
- `Pause engine`
- `Resume engine`
- `Stop engine`

## Recovery and Mishap Handling

1. Browser/window close is treated as UI detach.
2. On reopen, detect still-running engines and present reconnect/recover actions.
3. Surface stale/orphaned runtime states clearly and provide deterministic cleanup actions.

## Future Compatibility

Design is forward-compatible with multi-node testing by extending runtime identity to `(node_id, engine_id)` while keeping the UI mental model unchanged.
