# UI OpenAPI Route Migration Plan (#6410)

`quality/guardrails-baseline.json` ratchets every browser-facing `/api/*` route in
`src/issue_orchestrator/entrypoints/web*.py` that is absent from
`docs/api/ui-openapi.json` (`ui_openapi_routes:uncontracted:*`). The ratchet stops
*new* uncontracted routes; this document is the agreed order for retiring the
*existing* baseline, so cleanup lands as reviewable groups rather than one
regeneration that hides unrelated drift.

## What "contracted" means here

A route leaves the baseline only when **both** halves are done — the guardrail
checks both, and either half alone is drift:

1. `docs/api/ui-openapi.json` gains a `paths` entry whose `200`
   `application/json` schema is a `$ref` to a named component.
2. The FastAPI decorator declares `response_model=<GeneratedModel>` and the
   handler returns that model (not a bare `JSONResponse`), so the payload is
   validated on the way out instead of being an untyped dict.

Non-200 paths (`503 Orchestrator not running`, `404`) stay as `JSONResponse` —
the contract describes the success payload.

After editing the schema, regenerate and drop the now-satisfied baseline entries:

```bash
python scripts/generate_ui_contracts.py
python tools/quality_guardrails.py --check-stale   # confirm which entries went stale
```

`--prune` rewrites *every* stale entry in the baseline, including unrelated debt
other files happen to have shed. Use it only when `--check-stale` shows nothing
but this group's routes; otherwise delete exactly this group's
`ui_openapi_routes:uncontracted:*` keys so the baseline diff stays reviewable.

## Group order

Groups are ordered by *payload ownership clarity* — surfaces whose payloads are
already built by an extracted, testable function come first, because contracting
them is a schema + `response_model` change rather than a refactor.

| # | Group | Tracking issue | Routes | Status |
|---|-------|----------------|--------|--------|
| 1 | Dashboard status + diagnostics reads | #6415 | 11 | **done** |
| 2 | Session and log artifact reads | #6416 | 11 | pending |
| 3 | Operator action writes | #6417 | 11 | pending |
| 4 | Refresh / retry / reset / history | #6418 | 14 | pending |
| 5 | Settings, auth, and dev/test routes | #6419 | 8 | pending |
| 6 | Legacy issue timeline route | #6421 | 1 | pending — contract *or retire* |

Each group below enumerates its routes as inline-code `METHOD /path` tokens
written exactly as the baseline spells them.
`tests/unit/test_ui_openapi_migration_plan.py` parses those tokens and holds
this table to the live baseline: every pending route must still be an
`ui_openapi_routes:uncontracted:*` entry, every route of a **done** group must
be gone from it, no route may appear in two groups, no baseline entry may be
missing from every group, and each `Routes` count must match its section. Write
routes out one per line — a summarising phrase like "the eight `/api/session/*`
routes" is what let this table drift out of step with the baseline once already.

### Group 1 — dashboard status + diagnostics reads (#6415)

- `GET /api/status`
- `GET /api/excluded-issues`
- `GET /api/info`
- `GET /api/config`
- `GET /api/debug`
- `GET /api/doctor`
- `GET /api/blocked-issues`
- `GET /api/dependency-problems`
- `GET /api/stale-issues`
- `GET /api/failure-diagnosis/{issue_number}`
- `POST /api/issues/{issue_number}/audit`

First because `web_diagnostics_routes.py` already funnels each payload through a
single `_*_payload` builder, and the failure-diagnosis and issue-audit routes
return the *same* `SessionFailureDiagnosis.to_dict()` shape — one component, two
routes, which is the pattern the later groups repeat.

### Group 2 — session and log artifact reads (#6416)

- `GET /api/log/{issue_number}`
- `GET /api/log/local/{issue_number}`
- `GET /api/session/claude-log/{issue_number}`
- `GET /api/session/manifest/{issue_number}`
- `GET /api/session/orchestrator-log/{issue_number}`
- `GET /api/session/phases/{issue_number}`
- `GET /api/session/prompt/{issue_number}`
- `GET /api/session/review-artifact/{issue_number}`
- `GET /api/session/review-transcript/{issue_number}`
- `GET /api/session/terminal-recording/{issue_number}`
- `GET /api/session/worktree/{issue_number}`

Two log routes plus nine `/api/session/*` routes. These share
`web_session_routes.py` helpers (`session_manifest_response`,
`session_phases_response`) that the already-contracted `/api/dialog/*` routes
consume, so the components must be factored so the dialog payloads keep
composing over them.

### Group 3 — operator action writes (#6417)

- `POST /api/kill/{issue_number}`
- `POST /api/focus/{issue_number}`
- `POST /api/send/{issue_number}`
- `POST /api/finder/{issue_number}`
- `POST /api/prompt/{agent_type}`
- `POST /api/open-file`
- `POST /api/host/open-path`
- `POST /api/host/reveal-worktree/{issue_number}`
- `POST /api/bulk-kill`
- `POST /api/bulk-cancel-queued`
- `POST /api/shutdown`

Mostly `{ok, error}`-shaped acknowledgements; the work is
converging them onto one typed action-result component instead of eleven
ad hoc dicts, plus `requestBody` schemas.

### Group 4 — refresh / retry / reset / history (#6418)

- `GET /api/history`
- `POST /api/refresh`
- `POST /api/refresh/visibility`
- `POST /api/pause`
- `POST /api/resume`
- `POST /api/retry/{issue_number}`
- `POST /api/bulk-retry`
- `POST /api/bulk-deprioritize`
- `POST /api/reset-retry`
- `POST /api/unblock-retry`
- `POST /api/history/clear`
- `POST /api/history/dismiss/{issue_number}`
- `POST /api/issues/{issue_number}/refresh`
- `POST /api/issues/{issue_number}/retry-publish`

Largest group and the most behaviourally
sensitive (reset/retry state contract), so it comes after the action-result
component from group 3 exists to build on.

### Group 5 — settings, auth, and dev/test routes (#6419)

- `GET /api/settings`
- `POST /api/settings`
- `GET /api/milestones`
- `POST /api/issues`
- `GET /api/sse-token`
- `GET /api/events`
- `POST /api/test/create`
- `POST /api/test/cleanup`

`/api/events` is SSE, not JSON — that route needs a
guardrail decision (contract a non-JSON media type, or scope the rule to JSON
responses) rather than an invented payload.

### Group 6 — legacy issue timeline (#6421)

- `GET /api/timeline/{issue_number}`

Served by `web_issue_detail_routes.py`. Explicitly
"contract *or retire*": `/api/issue-detail/{issue_number}` already covers the
browser's timeline needs, so the first question is whether any caller remains.

## Rules for each cleanup PR

- One group per PR. Do not regenerate broadly; the diff should be the group's
  paths, the group's components, and the group's routes.
- Every new component needs a payload test that validates a *representative
  producer-built payload* — not a hand-written literal that only proves the
  schema validates itself. Component-level payload tests belong in
  `tests/unit/test_ui_openapi_payloads.py`; a group may additionally add one
  `tests/unit/test_ui_openapi_<group>_routes.py` file that drives each migrated
  route through `TestClient` and asserts the *wire* payload against both the
  JSON Schema and the generated model. Group 1 did this in
  `tests/unit/test_ui_openapi_status_diagnostics_routes.py`, and it is the
  stronger check of the two — a producer test cannot see response-model
  serialization settings, so only a route test catches wire drift such as an
  optional field newly serializing as `null`.
- Remove exactly the migrated `ui_openapi_routes:uncontracted:*` baseline
  entries, and leave the rest of the baseline untouched — never raise a
  baseline value to make a check pass.
- Preserve existing browser behavior. A wire-shape change is allowed only when
  called out explicitly in the PR body.

## Related

- #6385 — added the `ui_openapi_routes` guardrail and the initial baseline.
- #6337 — browser runtime validators (explicitly out of scope here).
- #6411 / #6412 / #6413 — guardrail hardening follow-ups (prefixed routers,
  stable dynamic metric IDs, missing-value diagnostics).
