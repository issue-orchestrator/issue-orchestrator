---
name: session-replay
description: Preserve the ui-session raw replay contract. Use when changing session artifacts, terminal recording capture, replay endpoints, timeline session actions, or the session viewer UI.
---

# Session Replay

Guardrails for the `ui-session` / `open_agent_log` path.

## When to Use

- Changing `terminal-recording.jsonl` production or lookup
- Changing the session viewer UI or modal behavior
- Changing timeline actions that open a session
- Changing replay endpoints or run-scoped artifact validation
- Investigating drift between timeline rows and the session viewer

## Non-Negotiable Design

`ui-session` exists to replay the raw run-scoped session output in a terminal emulator.

- Capture raw output for the selected run.
- View that raw output in a single emulator-backed viewer.
- Provide replay controls for after-the-fact inspection.
- Keep the selected timeline entry and selected session artifact run-scoped and identical.
- Treat empty or missing raw recordings as a correctness bug in capture/contract, not as a reason to switch the main view to some other log.

## Do Not Regress To

- Plain-text preview as the primary `ui-session` experience
- Fallback from raw session replay to unrelated provider logs
- Session actions that resolve by issue number while silently changing runs underneath the user
- Merging coding and review output into one undifferentiated timeline/session presentation

## Required Invariants

- The timeline action next to a session row opens the run that row represents.
- The viewer renders terminal behavior through the emulator, not through ad hoc text cleaning.
- Coding and review sessions remain distinct in both timeline presentation and replay selection.
- Replay controls are available for paused, resumed, and after-the-fact inspection.

## Required Tests

Add or update tests whenever this area changes.

- `tests/unit/test_manifest_accessor.py`
  Pin the canonical run-scoped session artifact lookup.
- `tests/unit/test_web.py`
  Pin action wiring, run-scoped endpoint validation, and replay endpoint behavior.
- `tests/unit/test_dashboard_ui_guardrails.py`
  Pin session action affordances and viewer-level guardrails.
- `tests/js/ui_action_contract.test.js`
  Pin frontend action ids, endpoint contracts, and replay viewer wiring.

If the change touches capture timing or artifact persistence, add the lower-layer regression where the bug actually lived before touching UI tests.

## Review Checklist

- Is the main session view still raw replay in an emulator?
- Does the clicked timeline row still map to the exact run being viewed?
- Are coding and review sessions still distinct?
- Did we avoid fallback behavior?
- Did we add both non-UI behavior coverage and UI guardrail coverage?
