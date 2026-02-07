---
name: control-center-lifecycle
description: Enforce Control Center vs Repository Engine lifecycle boundaries, terminology, and control placement when changing UI or lifecycle behavior.
---

# Control Center Lifecycle Skill

Use this skill when changing Control Center UI structure, lifecycle actions, or runtime state presentation.

## Source of Truth

1. `docs/architecture/control_center_lifecycle_model.md`
2. `docs/development/control_center_lifecycle_checklist.md`

## Required Rules

1. Treat **Control Center** as UI shell; treat **Repository Engine** as runtime.
2. Keep left nav stable (view selectors only), no state-driven entity insertion.
3. Keep global controls global; keep engine controls in engine surfaces.
4. Use explicit action labels: `Start/Pause/Resume/Stop engine`.
5. Use standardized state labels: `Running`, `Paused`, `Not running`.
6. Preserve paused observability; pause blocks execution, not visibility.
7. If event/payload shape changes, update contracts and tests in the same change.

## Workflow

1. Read the lifecycle model doc.
2. Apply the migration checklist to planned edits.
3. Implement UI/behavior changes with explicit scope wording.
4. Update tests and docs before finishing.

## Exit Criteria

- Terminology and action scope are unambiguous.
- No global/local control scope confusion remains in changed surfaces.
- Checklist items relevant to the change are complete.
