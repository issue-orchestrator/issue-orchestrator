---
name: ui-openapi
description: Manage the UI OpenAPI contract (view-model + dialog HTTP endpoints). Use when changing UI payloads/endpoints or regenerating contract artifacts.
---

# UI OpenAPI Contract

## When To Use

Use this skill when:
- You add or change UI HTTP endpoints under `/api/view-model`, `/api/issue-rows`, or `/api/dialog/*`.
- You add or change any UI payload fields consumed by the web UI.
- You need to regenerate or validate OpenAPI contract artifacts.

## Source Of Truth

- Canonical schema: `docs/api/ui-openapi.json`
- Generated artifacts:
  - Server models: `src/issue_orchestrator/contracts/ui_openapi_models.py`
  - Client types: `src/issue_orchestrator/static/js/ui-contracts.d.ts`

## Required Workflow

1. Edit the schema first: `docs/api/ui-openapi.json`.
2. Regenerate artifacts:
   - `./scripts/generate_ui_contracts.py`
3. Ensure `response_model` uses generated models in `src/issue_orchestrator/entrypoints/web.py`.
4. Run tests that enforce guardrails:
   - `tests/unit/test_ui_openapi_generated.py`
   - `tests/unit/test_ui_openapi_payloads.py`

## Do Not Modify Directly

Never edit these files by hand; they are generated and guarded by tests:
- `src/issue_orchestrator/contracts/ui_openapi_models.py`
- `src/issue_orchestrator/static/js/ui-contracts.d.ts`

If you need changes, edit `docs/api/ui-openapi.json` and regenerate.
