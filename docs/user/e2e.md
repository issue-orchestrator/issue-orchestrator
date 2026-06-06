# Async E2E Test Runner

The Issue Orchestrator can run long-lived E2E suites in a background worker and surface the results in the dashboard without making the product pytest-shaped.

The reporting model is:

1. Raw run output is always captured.
2. Structured case results are ingested from JUnit XML when the runner emits it.
3. Native framework artifacts such as HTML reports, traces, screenshots, and logs are linked as artifacts.
4. Agentic issue lifecycles, logical cycles, validation, and session logs appear as linked evidence when the tests exercised the orchestrator itself.

That gives one UI that works for both of these cases:

- `issue-orchestrator` running pytest-based agentic E2E tests
- external repos such as `tixmeup` running arbitrary commands that emit JUnit XML and artifacts

## Dashboard Model

Each E2E run now has two primary surfaces:

- `Results`: framework-neutral case outcomes, raw output, JUnit-backed results, and native artifacts
- `Timeline`: chronological run events plus linked issue lifecycles when the suite created or exercised issues

When an E2E run includes orchestrator work, the Results tab also shows `Linked issue lifecycles`. Those rows keep the semantically projected cycles visible and expose:

- `Timeline`
- `Coder Session`
- `Review Session`
- `Review Transcript`
- `Review Report`
- `Decision JSON`
- `Validation`

That is the critical bridge for agentic tests: a non-agentic suite is still debuggable from raw output and JUnit results, while an agentic suite additionally exposes logical cycles and UI session logs.

## Test Tiers

There are now two useful layers for onboarding and orchestration journeys:

- regular `tests/e2e` live coverage for issue pickup, session execution, review, and PR paths
- live agent transport acceptance for provider-dependent TUI contracts such as persistent prompt injection
- `heavy_e2e` journey coverage for broader flows such as onboarding, where a test may create a temp repo, run the setup wizard, install guardrails, and validate local doctor/guardrail behavior end to end
- an opt-in live agent-guided onboarding acceptance that lets a real `codex` or `claude-code` session onboard a GitHub-backed repo, then proves the first issue can launch

Run the heavy tier with:

```bash
make test-e2e-heavy
```

Keep this tier out of normal fast validation. It is intended for explicit runs, nightly coverage, or future provider-acceptance journeys.

Run the live agent-guided onboarding acceptance explicitly:

```bash
make test-e2e-onboarding-live

# Default provider is codex. To include Claude too:
E2E_AGENT_GUIDED_ONBOARDING_PROVIDERS=codex,claude-code make test-e2e-onboarding-live
```

The live onboarding acceptance is collection-gated behind `E2E_AGENT_GUIDED_ONBOARDING=1` so normal `heavy_e2e` runs do not burn GitHub cleanup calls just to skip it.

## Quick Start

### Pytest runner: issue-orchestrator style

Use this when the suite is already pytest-based and you want the dashboard to ingest structured per-case results in addition to runtime events.

```yaml
e2e:
  enabled: true
  role: "auto"
  runner_kind: "pytest"
  auto_run_interval_minutes: 30
  pytest_args:
    - "tests/e2e"
    - "-v"
    - "--junitxml=.issue-orchestrator/e2e-results/pytest-junit.xml"
  junit_xml_paths:
    - ".issue-orchestrator/e2e-results/pytest-junit.xml"
  allow_retry_once: true
  quarantine_file: "tests/e2e/quarantine.txt"
  auto_quarantine: true
  auto_create_issues: true
  issue_agent_label: "agent:backend"
  survive_restart: true
```

Notes:

- `pytest_args` still drive live pytest execution and retries.
- `junit_xml_paths` point at the files to ingest after the run completes.
- `Raw Output` is available even if the XML report is missing or incomplete.
- Pytest resume works best when long workflows are split into discrete test functions so already-passing nodeids can be deselected after an interruption.

### Command runner: framework-neutral mode

Use this when the suite lives behind a command such as Playwright, Vitest, Cypress, Robot Framework, or a project-local wrapper script.

```yaml
e2e:
  enabled: true
  role: "auto"
  runner_kind: "command"
  auto_run_interval_minutes: 30
  command:
    - "./scripts/run-e2e-suite.sh"
  junit_xml_paths:
    - "test-results/junit.xml"
  artifact_paths:
    - "playwright-report/index.html"
    - "test-results/**/*.zip"
    - "test-results/**/*.png"
  auto_create_issues: true
  issue_agent_label: "agent:backend"
```

The command runs inside the E2E worktree. Missing configured JUnit or artifact paths fail the run loudly.

## What To Configure For New Projects

For any new repo, start with these invariants:

1. The E2E command must produce a stable raw log. The orchestrator captures this automatically.
2. The suite should emit JUnit XML whenever practical.
3. Any native HTML report or trace output should be listed in `artifact_paths`.
4. Agentic issue/session data is optional. The dashboard works without it.

Recommended patterns:

- `pytest`: add `--junitxml=...` to `pytest_args` and mirror the same path in `junit_xml_paths`
- `Playwright`: emit JUnit XML plus `playwright-report/index.html`
- `Vitest` / `Jest`: use a JUnit reporter plus any native HTML/JSON output as artifacts
- project-local wrapper scripts: keep the command stable and have the script write JUnit + artifacts

## Results, Artifacts, And Session Logs

The Results tab intentionally separates universal debugging evidence from orchestrator-specific evidence.

Universal run evidence:

- canonical command
- run status
- started time
- duration
- `Raw Output`
- structured reports and additional artifacts

Agentic evidence, when present:

- linked issue lifecycles
- logical cycle chips
- coder/reviewer session recordings
- review transcript
- review report and decision JSON artifacts
- validation details

This matters because many E2E suites will not create issues on every failing test. In that case:

- the run is still debuggable from `Results`
- the Timeline still shows chronology
- linked lifecycle/session controls simply remain absent

## Retry And Resume Semantics

`runner_kind=pytest`

- supports live per-test progress
- supports retry-once
- interrupted pytest runs can resume through deselection of already-passing tests

`runner_kind=command`

- is framework-neutral
- ingests results after the command completes
- does not attempt pytest-style resume semantics
- interrupted runs restart fresh

## API Endpoints

The dashboard uses these endpoints:

Authenticated control API calls require a bearer token from
`~/.issue-orchestrator/api-token`, the target repo root, and `config_name`.
Older examples that omit `config_name` will no longer work.

- `POST /control/e2e/start`
- `POST /control/e2e/stop`
- `GET /control/e2e/status`
- `GET /control/e2e/runs`
- `GET /api/e2e-run-detail/{run_id}`
- `GET /control/e2e/run/{run_id}/timeline`
- `GET /api/e2e-run/{run_id}/issue-detail/{issue_number}`
- `GET /api/session/terminal-recording/{issue_number}`
- `GET /api/session/review-transcript/{issue_number}`
- `GET /api/session/review-artifact/{issue_number}`

`/api/e2e-run-detail/{run_id}` is the main typed payload for the run drawer. It carries:

- run metadata
- results summary
- categorized case results
- artifacts and reports
- lifecycle projection

## Database Model

Run metadata lives in `.issue-orchestrator/e2e.db`.

Key tables:

- `e2e_runs`
- `e2e_test_results`
- `e2e_run_artifacts`

Important run fields:

- `pytest_args`
- `command_json`
- `runner_kind`
- `log_path`
- `artifacts_dir`

Important case fields:

- `display_name`
- `suite_name`
- `result_source`
- `outcome`
- `longrepr`

`result_source` tells you whether a row came from live runtime observation or a structured external report such as JUnit XML.

## Debugging

### Check recent runs

```bash
sqlite3 .issue-orchestrator/e2e.db "
  SELECT id, runner_kind, status, started_at, command_json
  FROM e2e_runs
  ORDER BY id DESC
  LIMIT 5
"
```

### Check the latest structured case results

```bash
sqlite3 .issue-orchestrator/e2e.db "
  SELECT nodeid, suite_name, outcome, result_source
  FROM e2e_test_results
  WHERE run_id = (SELECT MAX(id) FROM e2e_runs)
  ORDER BY nodeid
"
```

### Check captured artifacts

```bash
sqlite3 .issue-orchestrator/e2e.db "
  SELECT kind, label, path
  FROM e2e_run_artifacts
  WHERE run_id = (SELECT MAX(id) FROM e2e_runs)
  ORDER BY kind, label
"
```

### Tail raw output

```bash
ls -lt .issue-orchestrator/logs/e2e/ | head -5
tail -f .issue-orchestrator/logs/e2e/run_*.log
```

## Common Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| E2E not auto-triggering | `auto_run_interval_minutes: 0` | Set it to a positive value |
| Results tab has only Raw Output | No JUnit XML configured or emitted | Add `--junitxml` / `junit_xml_paths` or configure your command runner to emit JUnit |
| Run fails with configured-report error | `junit_xml_paths` or `artifact_paths` did not resolve | Fix the emitted file path or the config glob |
| Command runner cannot resume | Resume is pytest-only | Restart the command run; use raw output and JUnit for debugging |
| Linked issue lifecycle is missing | The suite did not create or exercise issues in-window | Debug from Results/Timeline instead; lifecycle/session controls are additive |
| Session Recording button does nothing | The lifecycle command lacked valid run-scoped recording context | This is a bug; the dashboard should only emit phase-scoped session parameters when both `round_index` and `session_role` are available |

## Auto-Trigger Logic

E2E auto-triggers when all conditions are met:

1. `e2e.enabled: true`
2. `auto_run_interval_minutes > 0`
3. enough time passed since the last run
4. the tracked main branch HEAD changed
5. no E2E run is already active
