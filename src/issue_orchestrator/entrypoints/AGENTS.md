# Entrypoints

**Purpose**: User-facing interfaces - CLI commands, web server, agent tools, workers.

**Boundaries**:
- Thin layer: parse input, call into control/domain, format output
- No business logic - delegate to control layer
- `cli_tools/` contains tools agents use (e.g., `agent-done`)
- Web endpoints serve the dashboard and API

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
