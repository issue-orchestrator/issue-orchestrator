# Control Center Lifecycle Migration Checklist

## Information Architecture

- [ ] Left nav items are stable view selectors only.
- [ ] No runtime-only entity rows are injected into nav.
- [ ] View titles clearly indicate mode (`Repository Engines`, `Repository Engine`, etc.).

## Terminology

- [ ] UI copy uses `Control Center`, `Repository Engine`, and `Jobs` consistently.
- [ ] Ambiguous `Shutdown` copy removed from engine scope actions.
- [ ] State labels standardized to `Running`, `Paused`, `Not running`.

## Control Placement

- [ ] Global header contains only global actions + aggregate status.
- [ ] Engine lifecycle controls are in engine cards/detail.
- [ ] Config selection is per-engine, not global.

## Lifecycle UX

- [ ] Visible note that closing Control Center does not stop engines.
- [ ] Engine detail supports paused observability (queue/history/blocked/logs).
- [ ] Stop action is explicit and clearly scoped.

## Recovery

- [ ] Reopen flow identifies detached/running engines.
- [ ] Recovery UI offers reconnect/stop actions.
- [ ] Stale/orphaned runtime cleanup path is defined.

## Contracts and Events

- [ ] UI reacts to lifecycle events, not logs.
- [ ] Lifecycle event names and payload scope are engine-specific.
- [ ] If payload shapes changed, regenerate and validate contracts.

## Tests and Docs

- [ ] Update UI tests for labels and action placement.
- [ ] Add/adjust lifecycle tests for close/reopen behavior.
- [ ] Keep architecture doc and checklist aligned with shipped behavior.
