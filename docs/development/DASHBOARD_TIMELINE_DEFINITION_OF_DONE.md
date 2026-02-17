# Dashboard Timeline/Actions Definition Of Done

Scope: dashboard timeline/action reliability and lane correctness.

## Checklist

- [ ] No empty context menus for compact cards across `blocked`, `awaiting-merge`, and `completed` columns.
- [ ] No ambiguous diagnostics/session-log actions; run-scoped actions include `run_dir`.
- [ ] Latest-run artifacts are usable before run-scoped log actions are offered.
- [ ] Open PR issues are routed to `Awaiting Merge`, not `Completed`.
- [ ] Tests enforce the above invariants.

## Verification Commands

```bash
cd /Users/brucegordon/dev/issue-orchestrator-wt-ui-iteration
pytest tests/unit/test_dashboard_ui_guardrails.py -q
pytest tests/unit/test_dashboard_view_model.py -q
pytest tests/integration/test_timeline_integration.py -q
pytest tests/integration/test_manifest_accessor.py -q
node --check src/issue_orchestrator/static/js/dashboard.js
```

## Contract Guardrails

- API action wiring: `tests/unit/test_web.py::TestTimelineActionWiring`
- Run-scoped timeline action invariants: `tests/integration/test_timeline_integration.py`
- Diagnostics view-model run-scope + actions: `tests/unit/test_dialog_view_models.py`
