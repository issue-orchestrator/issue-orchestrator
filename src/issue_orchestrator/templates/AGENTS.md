# UI Reliability Rules

For UI-facing changes under `templates/` and `static/`:
- UI is adapter-only: keep action semantics/policy below UI.
- Task is incomplete without non-UI behavior tests (domain/API) and UI guardrail tests.
- When fixing a UI bug, add a lower-layer regression test in the same change.
- Accessibility is part of the bug fix: preserve semantic HTML, keyboard access, visible focus, accessible names, labelled expanded regions, contrast, and unclipped text/focus/content.
- Reviewers must call out accessibility regressions as required fixes; if none are found, say `Accessibility review: no issues found.`
