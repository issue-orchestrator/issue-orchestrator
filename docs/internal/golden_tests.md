# Golden tests (must stay green)

- `tests/unit/test_github_http.py::test_get_issue_uses_etag_cache`
- `tests/unit/test_action_applier.py::test_reconciliation_disabled`
- `tests/unit/test_action_applier.py::test_reconciliation_enabled`
- `tests/unit/test_action_applier.py::test_expected_not_enforced_when_reconcile_disabled`
- `tests/unit/test_arch_guardrails.py::test_blocks_subprocess_import_in_control`
- `tests/unit/test_arch_guardrails.py::test_allows_subprocess_in_execution`
- `tests/unit/test_arch_guardrails.py::test_blocks_dynamic_import`
- `tests/unit/test_fact_gatherer.py::test_create_snapshot_paused_state`
- `tests/unit/test_workflows.py::test_should_launch_skips_when_paused`
- `tests/unit/test_prepush_check.py::test_returns_0_when_validation_passes`
- `tests/unit/test_prepush_check.py::test_returns_1_when_validation_fails`
- `tests/e2e/test_inflight_refresh_discovers_issue.py::test_inflight_refresh_discovers_issue`
