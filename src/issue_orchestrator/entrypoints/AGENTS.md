# Entrypoints

**Purpose**: User-facing interfaces - CLI commands, web server, agent tools, workers.

**Boundaries**:
- Thin layer: parse input, call into control/domain, format output
- No business logic - delegate to control layer
- `cli_tools/` contains tools agents use (e.g., `coding-done`, `reviewer-done`)
- Web endpoints serve the dashboard and API

## Run-Asset Entrypoint Contract

- Entrypoints that participate in active session completion must require the
  owner-injected typed run contract. Missing `ISSUE_ORCHESTRATOR_RUN_DIR` in an
  orchestrator-managed session is a hard error, not a reason to search.
- CLI and HTTP handlers should parse external input once, construct typed
  command/value objects, and pass those into owners. Do not pass loose metadata
  maps or optional required fields through the active path.
- Completion tools may create fresh run assets only for standalone developer
  invocations. Managed sessions must use the run assets injected by the
  orchestrator owner.

## Key Files

| File | Purpose |
|------|---------|
| `cli.py` | Main CLI commands (start, status, doctor) |
| `web.py` | Dashboard and SSE endpoints |
| `control_api.py` | Control API including `/control/e2e/*` endpoints |
| `e2e_worker.py` | Subprocess entrypoint for async E2E test runner |

## E2E Worker

`e2e_worker.py` runs pytest in a subprocess with a custom plugin that streams results to SQLite:

```bash
python -m issue_orchestrator.entrypoints.e2e_worker \
  --repo-root /path/to/repo \
  --db-path /path/to/.issue-orchestrator/e2e.db \
  --orchestrator-id my-orch \
  --pytest-args-json '["tests/e2e", "-v"]' \
  --allow-retry-once
```

The `ResultPlugin` captures test outcomes via `pytest_runtest_logreport` hook.
