# Generated UI Contract Artifacts

Do not edit the generated files directly. They are produced from the canonical
schema in `docs/api/ui-openapi.json` and are guarded by tests.

Generated files:
- `src/issue_orchestrator/contracts/ui_openapi_models.py` — server-side Pydantic models
- `src/issue_orchestrator/static/js/ui-contracts.d.ts` — client-side types (build-time)
- `src/issue_orchestrator/static/js/ui-contracts.validators.js` — browser runtime
  validators (issue #6337). Browser code reaches these through the shared readers
  in `static/js/ui_contract_json.js`; see
  `docs/architecture/browser-json-contracts.md`.

The generator refuses to emit a validator for a JSON Schema keyword the browser
engine cannot enforce, rather than silently under-validating. To add one, teach
`_VALIDATOR_ENGINE` in `ui_openapi_generator.py` and add the keyword to
`SUPPORTED_SCHEMA_KEYWORDS`.

To make changes, update the schema and regenerate:
```
.venv/bin/python scripts/generate_ui_contracts.py
```
