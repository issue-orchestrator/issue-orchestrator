---
name: frontend-design
description: Apply frontend design principles to UI components. Use when working on templates/, static files, or any user-facing HTML/CSS/JS. Ensures information hierarchy, user action flow, and accessible progressive disclosure.
---

# Frontend Design Skill

This skill provides design principles for UI work in issue-orchestrator.

## When to Use

- Modifying templates in `templates/`
- Working on control center UI
- Adding new user-facing components
- Improving user action flows
- Decisions about what to show vs. hide

## Core Principles

### 1. Information Hierarchy

**Most important elements should be most visible.** Design the visual hierarchy to guide user attention naturally.

| Visibility Level | What Belongs Here | Examples |
|------------------|-------------------|----------|
| Always visible | Primary actions, common cases | Start/Stop, main status |
| One click away | Power-user features, advanced options | Multi-repo table, settings |
| Hidden/modal | Rare actions, destructive operations | Shutdown, delete |

### 2. Progressive Disclosure

**Show complexity only when needed.** Don't overwhelm users with options they rarely use.

```
Good: [Start] [Status] [All Repositories ▼]
                           └─ expands to show advanced table

Bad: Start | Stop | Pause | Status | Config | Repos | Settings | ...
```

Rules:
- Default state should handle 80% of use cases
- Advanced features one click away with clear labels
- Labels describe what's revealed, not that it's "advanced"

### 3. Labeling Clarity

**Labels should describe content, not difficulty level.**

| Instead of... | Use... | Why |
|---------------|--------|-----|
| "Advanced" | "All Repositories" | Describes what's shown |
| "Expert Mode" | "Developer Tools" | Describes the content |
| "More Options" | "Notification Settings" | Specific to what expands |

### 4. User Action Flow

**Primary actions should be reachable in minimal clicks.**

For issue-orchestrator specifically:
1. **Starting an orchestrator** - Should be 1 click from main view
2. **Viewing status** - Should be always visible
3. **Finding repos to manage** - Discovered repos should be visible
4. **Multi-repo management** - OK to be one click away (power user feature)

### 5. Consistency

- Use the same terminology throughout (`repo` not sometimes `repository`)
- Use consistent button styles for similar actions
- Position similar controls in predictable locations

### 6. Action Semantics Verification (Required)

UI buttons are adapters, not policy owners.

- Define/extend a **UI action contract** before wiring new buttons:
  - action id
  - endpoint + payload shape
  - expected state transition/invariants
- Verify action behavior primarily below UI:
  - domain/policy unit tests (pure logic)
  - API behavior tests (state + labels + eligibility effects)
- Keep UI tests focused on:
  - wiring to the contract
  - enable/disable affordance rules
  - rendering parity (compact vs expanded, etc.)

When a bug is found in UI behavior, add a lower-layer regression test first, then patch UI wiring.

## Control Center Layout Reference

```
Header: [All Repositories] [+ New Setup] [Build Info] [Shutdown]

Main Content (always visible):
  ┌─────────────────────────────────────────────┐
  │ Discovered Repositories                      │  ← User entry point
  └─────────────────────────────────────────────┘
  ┌─────────────────────────────────────────────┐
  │ Orchestrator (Primary)                       │  ← Main status/control
  └─────────────────────────────────────────────┘

Hidden by default (toggle reveals):
  ┌─────────────────────────────────────────────┐
  │ Registered Repositories (table)              │  ← Power-user multi-repo
  └─────────────────────────────────────────────┘
```

## Key Files

- `src/issue_orchestrator/templates/control_center.html` - Main UI
- `src/issue_orchestrator/static/` - Static CSS, JS, and vendor assets

## Browser Auth Contract

For dashboard or Control Center actions that call authenticated endpoints:

- Load `static/js/browser_auth.js` before feature-specific scripts.
- Render `<meta name="io-csrf-token" content="{{ csrf_token }}">` on authenticated HTML pages.
- Render `<meta name="io-browser-auth-required" content="1|0">` so dev/test no-auth pages do not request SSE tokens.
- Use normal `fetch`; the shared helper owns `X-CSRF-Token` injection and 401 reload behavior.
- Use `window.openAuthenticatedSseStream('/api/events')` for EventSource connections; never open authenticated SSE paths directly.
- Check `response.ok` on mutating actions and surface failures in the UI instead of optimistic state changes.
- For route/UI tests, use `fake_browser_auth` or the `auth_enabled_*` / `logged_in_dashboard_client` fixtures so auth is real but deterministic.

## Review Artifact UI

For review artifacts, keep the human-readable report as the primary visible action and place the decision JSON behind a secondary/menu action. Use native buttons with accessible names, visible focus, and the shared UI action contract. Render markdown safely; never inject raw artifact HTML.

## Checklist for UI Changes

- [ ] Does the change preserve information hierarchy?
- [ ] Are labels descriptive of content (not difficulty)?
- [ ] Is the primary user flow still minimal clicks?
- [ ] Does the default state serve the common case?
- [ ] Are advanced features clearly labeled but not hidden behind vague terms?
- [ ] Is there a UI action contract entry for each changed button/action?
- [ ] Are action semantics verified by non-UI tests (domain/API), not just UI tests?
- [ ] Do compact and expanded views use the same policy and display semantics?
- [ ] Do authenticated UI actions use the shared browser auth helper and handle failed responses?
