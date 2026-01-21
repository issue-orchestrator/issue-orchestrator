# Blocked Issues UX Improvement Ideas

## The Problem

Users have 3 blocked issues but:
- "Needs Attention" shows 0
- Issues are buried in History tab
- "Manage Blocked" button doesn't appear
- "Launch Debug" is inaccessible

**Root cause:** UI is organized around *session lifecycle* (active vs finished) instead of *what the user needs to do*.

## User Mental Model

Users think in terms of:
1. **"What's happening?"** - Work in progress
2. **"What needs me?"** - Blocked, needs human help
3. **"What's done?"** - Completed successfully

They don't think: "Is the terminal session still running?"

---

## Idea 1: Always-Visible Blocked Counter

**Concept:** Blocked issues are too important to hide. Show them prominently, always.

```
┌─────────────────────────────────────────────────────────────┐
│ Issue Orchestrator  [RUNNING]          [🔴 3 Blocked]  [≡]  │
└─────────────────────────────────────────────────────────────┘
```

- Red badge in header shows count of ALL blocked issues (active + history)
- Click opens the Blocked modal immediately
- Never goes to zero unless truly no blocked issues exist
- Pulsing/attention-grabbing when count increases

**Why it's good:** User always knows their attention is needed. No hunting through tabs.

---

## Idea 2: Rename and Redefine Tabs

**Current (confusing):**
- Work | Needs Attention | History

**Proposed:**
- Active | Blocked | Completed

| Tab | Shows | Empty State |
|-----|-------|-------------|
| **Active** | Sessions currently running | "No active sessions" |
| **Blocked** | ALL issues needing human help | "Nothing blocked!" |
| **Completed** | Successfully finished work | "No completions yet" |

**Key change:** "Blocked" tab shows issues regardless of session state. If agent said "blocked", it's here until resolved.

---

## Idea 3: Unified Timeline with Status Badges

**Concept:** Single scrollable list, every issue shows its current state clearly.

```
┌─────────────────────────────────────────────────────────────┐
│ #3895  Add unit tests for completion_observer...            │
│ [IN PROGRESS → BLOCKED]  validation_failed  9 min          │
│                                    [Debug] [Retry] [Dismiss]│
├─────────────────────────────────────────────────────────────┤
│ #3894  Fix login bug                                        │
│ [COMPLETED ✓]  PR #401 merged  12 min                       │
│                                           [View PR] [Close] │
└─────────────────────────────────────────────────────────────┘
```

- No tabs, just filters: [All] [Active] [Blocked] [Done]
- Actions always visible on each row
- Status progression shown inline

**Why it's good:** Everything in one place. No wondering "which tab is this in?"

---

## Idea 4: Action Cards for Blocked Issues

**Concept:** When issues are blocked, show them as action cards at the top of the dashboard.

```
┌─────────────────────────────────────────────────────────────┐
│ 🚨 3 issues need your attention                             │
├─────────────────────────────────────────────────────────────┤
│ ┌─────────────────────┐ ┌─────────────────────┐             │
│ │ #3895               │ │ #3896               │  ...        │
│ │ validation_failed   │ │ validation_failed   │             │
│ │ [Launch Debug]      │ │ [Launch Debug]      │             │
│ └─────────────────────┘ └─────────────────────┘             │
└─────────────────────────────────────────────────────────────┘
│                                                             │
│ Active Work                                                 │
│ (empty - all sessions blocked)                              │
│                                                             │
│ Recent Completions                                          │
│ ...                                                         │
```

- Blocked issues ALWAYS at top, impossible to miss
- One-click "Launch Debug" right there
- Cards collapse/dismiss when resolved
- Active work and history below

**Why it's good:** The most important thing (blocked issues) gets prime real estate.

---

## Idea 5: Quick Actions Everywhere

**Problem:** "Launch Debug" is buried in a modal that only appears sometimes.

**Solution:** Make key actions available anywhere an issue appears:

| Context | Available Actions |
|---------|-------------------|
| Issue row (any tab) | [Debug] [Retry] [Dismiss] [GitHub] |
| Phase indicator click | [Debug] [View Logs] [Diagnose] |
| Context menu (right-click) | All actions |

**Implementation:**
- Add action buttons to the right side of every issue row
- "Debug" button visible whenever worktree exists
- Don't require opening modals for common actions

---

## Idea 6: Smart Notifications

**Concept:** Proactively tell users when things need attention.

- Toast notification when an issue becomes blocked
- Browser notification (optional) for blocked issues
- Sound alert (optional) for failures
- Summary notification: "3 issues blocked in the last hour"

**Settings:**
```
Notifications:
  [x] Show toast when issue blocks
  [ ] Browser notifications
  [ ] Sound alerts
```

---

## Idea 7: Issue Journey Visualization

**Concept:** Show the path each issue took through the system.

```
#3895: Add unit tests for completion_observer

  Queued ──→ Started ──→ In Progress ──→ Blocked
    │          │            │              │
  2:30pm    2:31pm       2:35pm         2:40pm
                                           │
                                    validation_failed
                                    "pytest found 3 errors"

  [View Full Log] [Launch Debug] [Retry from Start]
```

**Why it's good:** Users understand exactly where things went wrong and when.

---

## Recommendation: Start Simple

**Phase 1 (Quick wins):**
1. Always-visible blocked counter in header
2. Fix worktree_path so "Launch Debug" works from history
3. Add "Debug" button to issue rows in History tab

**Phase 2 (Improve clarity):**
1. Rename tabs: Active | Blocked | Completed
2. Blocked tab shows ALL blocked issues regardless of session state
3. Action buttons on every issue row

**Phase 3 (Polish):**
1. Action cards at top of dashboard
2. Toast notifications for state changes
3. Issue journey visualization

---

## Questions to Consider

1. Should "Blocked" be a tab or just a persistent modal/panel?
2. Should resolved blocked issues stay visible briefly or disappear immediately?
3. How prominent should the blocked counter be? Header? Floating badge?
4. Should "Launch Debug" auto-focus the terminal, or just open it?
