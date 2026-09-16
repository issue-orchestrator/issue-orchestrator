# Browser Runtime Validation for UI JSON

JSON that crosses into browser JavaScript from a UI boundary is validated
at runtime against the generated contract before feature code sees it.

Source of truth: `docs/api/ui-openapi.json`.

## Ownership Boundary

Two different kinds of invariant, two different owners. Keep them apart.

| Owner | Owns | Examples |
|-------|------|----------|
| **Generated validators** (`static/js/ui-contracts.validators.js`) | JSON **payload shape** | `kind` is a known variant; `run_id` is an integer ≥ 1; required fields present; no unknown fields; enums in range |
| **UI owner abstractions** | **Contextual** invariants a schema cannot know | `resolveRowCommandContext()` proving a command's `run_id` matches the `details[data-e2e-run-id]` row that dispatched it |

A schema can say "`run_id` is a positive integer". It cannot say "this
`run_id` is the row the user actually clicked". The first belongs to the
contract; the second belongs to the module that holds the DOM context.

## The Layer

```
docs/api/ui-openapi.json           ← canonical schema (edit this)
        │  scripts/generate_ui_contracts.py
        ├──> contracts/ui_openapi_models.py        (server: Pydantic)
        ├──> static/js/ui-contracts.d.ts           (client: types, build-time)
        └──> static/js/ui-contracts.validators.js  (client: validators, RUN-time)
                     │
                     └──> static/js/ui_contract_json.js   ← the shared reader
                                  │
                                  └──> feature modules receive validated payloads
```

`ui-contracts.validators.js` carries a projection of the schema plus an
engine that enforces it. `ui_contract_json.js` is the fail-closed reader
every JSON boundary calls.

### Reading JSON in browser code

Never `JSON.parse` a contract-covered payload, and never call
`validators.validate` directly — go through a reader so the fail-closed
behaviour and the diagnostics stay in one place:

```js
// DOM data-* payload
const command = uiContractJson.fromDataset(el, 'lifecycleCommand', 'TimelineCommandPayload');
if (!command) return;                       // rejected: already reported

// inline <script type="application/json">
const payload = uiContractJson.fromInlineScript(node, 'RecentE2ERunsPayload');

// fetch response (HTTP status stays the caller's concern)
const payload = await uiContractJson.fromResponse(res, 'RecentE2ERunsPayload', '/api/e2e-runs/recent');

// event / stream data
const payload = uiContractJson.fromEventData(event.data, 'SomePayload', 'sse:lifecycle');

// already-decoded object
const payload = uiContractJson.fromValue(obj, 'SomePayload', 'inline bootstrap');
```

Every reader returns the payload or `null`. `null` means "rejected, and
one diagnostic was already reported" — console error plus a toast when the
shell provides one. Tests redirect diagnostics with
`setViolationReporter()`.

**Fail closed**: on `null`, return before mutating UI state or issuing a
request. A rejected payload must not half-render.

### Adding a keyword to the schema

The generator enforces only what the browser engine can enforce. A JSON
Schema keyword outside `SUPPORTED_SCHEMA_KEYWORDS` fails generation rather
than being silently ignored — a validator that quietly skips `pattern`
would be worse than none, because callers would trust it. To add one:
teach `_VALIDATOR_ENGINE` in `contracts/ui_openapi_generator.py`, then
list the keyword.

### Parity with the server model

`tests/unit/test_ui_openapi_generated.py` pins the directional invariant:
**the browser must never accept a payload the generated Pydantic model
rejects.** The converse is allowed — the browser checks wire types
strictly (`"88"` is not `88`, `true` is not `1`) while Pydantic's lax mode
coerces some scalars. Stricter at the browser boundary is intended.

## Inventory of Browser JSON Ingestion Points

Classification (issue #6337):

- **contract-covered** — validated by the generated validators.
- **contract-missing** — should be under contract; not yet.
- **context-local** — depends on DOM/user/runtime state a schema cannot express.
- **raw-json-intentional** — no contract is useful; documented why.

The `fetch` rows below are enforced, not just documented.
`tests/unit/test_dashboard_ui_guardrails.py` derives the set of
contract-covered endpoints from `docs/api/ui-openapi.json` and fails if a
browser JS file touches one without either reading it through
`uiContractJson.fromResponse(...)` (naming the schema the contract
assigns to that endpoint) or being recorded as a known follow-up. The
follow-up list is a ratchet: it may shrink, never grow.

### Dashboard (`static/js/dashboard/`, `static/js/`)

| Site | Boundary | Class | Status |
|------|----------|-------|--------|
| `lifecycle_commands.js` — `data-lifecycle-command` | DOM `data-*` | contract-covered | Validated as `LifecycleCommandPayload`, the generated union of timeline and dialog commands |
| `lifecycle_commands.js` — `runLifecycleCommand(cmd)` | in-JS callers | contract-covered | Validated at the dispatcher, so JS-built commands are held to the same contract |
| `e2e_runs_list.js` — `#recentE2ERunsData` | inline `<script>` | contract-covered | Validated as `RecentE2ERunsPayload` |
| `e2e_runs_list.js` — `/api/e2e-runs/recent` | `fetch().json()` | contract-covered | Validated as `RecentE2ERunsPayload` |
| `core.js` — `/api/view-model`, `/api/view-model-snapshot` | `fetch().json()` | contract-covered | Validated as `DashboardViewModelPayload` / `ViewModelSnapshotPayload`; replaced a hand-written `!payload.view_model \|\| !Array.isArray(payload.rows)` check |
| `core.js` — `/api/issue-rows` | `fetch().json()` | contract-covered | Validated as `IssueRowsPayload`; replaced a `data.rows \|\| []` fallback that emptied the issue list on a malformed body |
| `e2e_run_view.js` — `/api/e2e-run-detail/{run_id}` | `fetch().json()` | contract-covered | Validated as `E2ERunDetailPayload` in `_fetchE2ERunDetail`, the **single owner** of this read; replaced a `typeof payload === 'object'` check |
| `e2e_runs_list.js` — run detail (`loadE2ERunIntoRow`) | delegated | contract-covered | Delegates to `_fetchE2ERunDetail`; previously duplicated the fetch and swallowed a malformed body into `{}` |
| `validation_viewer.js` — `/api/e2e-run/{run_id}/test-output` | `fetch().json()` | contract-covered | Validated as `E2ETestOutputPayload`; replaced a `.catch(() => ({}))` that rendered "no captured output" for a malformed body. The URL arrives via `data-cvv-output-url`, so the guardrail's endpoint scan cannot see it — the binding is pinned explicitly |
| `e2e_run_view.js`, `validation_viewer.js` — error bodies | `fetch()` error body | raw-json-intentional | The contract defines the 200 only, so a failure body has no schema; yields a display string, never a payload the renderer sees |
| `core.js` — `/api/info` | `fetch().json()` | contract-missing | `OrchestratorInfoPayload` exists; its reader is not yet applied. Follow-up |
| `e2e_run_view.js` — `/control/e2e/create-issues/{run_id}` | `fetch().json()` | raw-json-intentional | Not in `ui-openapi.json`; no component to validate against |
| `e2e_canonical_payload.js`, `ui_action_contract.js`, `flash_debug.js` — endpoint names | not a JSON read | n/a | Name a contract-covered endpoint without reading its body: a URL builder, a URL registry, and a `fetch` timing probe respectively |
| `e2e_run_view.js` — `resolveRowCommandContext()` | DOM row identity | context-local | Stays local: proves a *valid* command targets the row that dispatched it |
| `timeline.js` — `dataset.action` (`runTimelineEventAction`) | DOM `data-*` | contract-missing | Legacy action payload keyed by `type`, not `kind`; no OpenAPI component. Follow-up |
| `shell_actions.js`, `session_dialogs.js`, `issue_detail_modals.js`, `plugins/agent_context.js` — `/api/dialog/*` | `fetch().json()` | contract-missing | Components exist (`InfoDialogPayload`, `ConfigDialogPayload`, `DebugDialogPayload`, `DoctorDialogPayload`, `PhaseDialogPayload`, `BlockedIssuesDialogPayload`, `SessionDiagnosticsDialogPayload`, `ValidationFailureDialogPayload`); readers not yet applied. Follow-up |
| `issue_detail_drawer.js`, `inline_agent_attempts.js`, `timeline.js` — `/api/issue-detail/*`, `/api/e2e-run/{run_id}/issue-detail/*` | `fetch().json()` | contract-missing | `IssueDetailPayload` exists; readers not yet applied. Follow-up |
| `e2e_canonical_payload.js` — `/api/e2e-run/{run_id}/test-output` | `fetch().json()` | contract-missing | `E2ETestOutputPayload` exists; reader not yet applied. Follow-up |
| `kanban_columns.js` — `/api/view-model` | `fetch().json()` | contract-missing | `DashboardViewModelPayload` exists; reader not yet applied. Follow-up |
| `controls_refresh.js`, `diagnostics_actions.js`, `issue_metadata.js` — contract-covered endpoint reads | `fetch().json()` | contract-missing | Their response components exist; these current-main boundaries postdate the original #6337 work and still need the shared reader. Follow-up |
| `ui_action_contract.js` — `/api/retrospective-review`, `/api/retrospective-review/preflight` | `fetch().json()` | contract-missing | Components exist; readers not yet applied. Follow-up |
| `issue_metadata.js` — SSE `e.data` (6 sites) | EventSource | contract-missing | SSE payloads are defined in `contracts/public.py`, not `ui-openapi.json`; needs a contract before a reader. Follow-up |
| `issue_menus.js` — `dataset.labels`, `dataset.dependencies` | DOM `data-*` | contract-missing | Bare JSON arrays with no component. Follow-up |
| `dashboard.html` — `window.dashboardData` | inline bootstrap | contract-missing | Mirrors `DashboardDataPayload`; assigned to a global rather than read through a reader. Follow-up |
| `settings_form_controls.js` — `dataset.valueOptions`, `dataset.initial` | DOM `data-*` | raw-json-intentional | Driven by the **settings** schema (`infra/settings_schema.py`), a separate contract surface from the UI OpenAPI one |
| `kanban_columns.js`, `compact_card_state.js`, `expanded_column_state.js`, `control_center.js` — `localStorage` reads | browser-local | raw-json-intentional | Browser-local UI state, never crosses a UI boundary; corrupt data degrades to defaults |
| `flash_debug.js` — SSE `e.data` | debug probe | raw-json-intentional | Opt-in diagnostic probe, not a product surface |

### Control Center (`static/js/control_center*.js`)

The UI contract now includes Control Center status, setup, worktree-audit,
and validated-work recovery responses. Their existing readers predate the
browser validator and remain classified follow-ups. `control_center_setup_commands.js`
only builds typed request descriptions and does not read response bodies.
