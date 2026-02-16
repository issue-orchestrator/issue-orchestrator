# UI Audit Checklist

Use this checklist for every dashboard/control-center UI iteration.

## 1. Action Wiring Correctness

- [ ] Every visible action button maps to the intended API endpoint.
- [ ] Unblock actions use `/api/unblock-retry` (not `/api/bulk-retry`).
- [ ] Destructive actions include explicit confirmation.
- [ ] Local-only actions (for example viewed-state toggles) do not call unrelated APIs.

## 2. Affordance and State

- [ ] Bulk action bars communicate availability clearly.
- [ ] Buttons that require selection are disabled when selection is empty.
- [ ] Disabled visual styling matches disabled semantics (`disabled` attribute, not style-only).
- [ ] Focus/hover/pressed states are visually consistent across dashboard and control center.

## 3. Refresh and Rendering Behavior

- [ ] Single-issue refresh updates only the affected card/row unless model data actually changed for others.
- [ ] Expanded column views use incremental updates and preserve selection where possible.
- [ ] Main issue rows use delta updates (no full-list repaint on no-op refresh).
- [ ] Compact card columns use per-card fingerprinting and replacement, not wholesale rebuilds.

## 4. Data Clarity

- [ ] Card badges show labels needed for user understanding (including `agent:*` routing labels).
- [ ] Blocking reason text is understandable and matches current labels/state.
- [ ] UI and GitHub state are consistent after action completion (for example unblock removes blocking labels).

## 5. Layout and Positioning

- [ ] Confirmation popovers/modals are fully visible in viewport and not clipped.
- [ ] Mobile and desktop both keep primary actions reachable.
- [ ] No critical action appears/disappears unexpectedly due to incidental cursor movement.

## 6. Logging and Noise

- [ ] Access logs are at appropriate verbosity for default use.
- [ ] Debug logging can be enabled explicitly without code edits.

## 7. Regression Guardrails

- [ ] JS unit tests pass (`tests/js/*`).
- [ ] UI wiring guardrail tests pass.
- [ ] Template rendering tests pass for key columns and controls.

## 8. Manual Scenario Pass

- [ ] Start control center -> open engine -> expand blocked -> select/deselect -> verify button behavior.
- [ ] Run single unblock from blocked card.
- [ ] Run bulk unblock from blocked expanded list.
- [ ] Open issue detail drawer and unblock from drawer.
- [ ] Use per-card refresh and verify no unrelated card visual reset.
