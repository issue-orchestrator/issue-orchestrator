"""GH audit budget check for e2e runs."""

from __future__ import annotations

import json
import os

import pytest

from issue_orchestrator import gh_audit
from tests.e2e.conftest import E2E_LOG_DIR

@pytest.mark.e2e
@pytest.mark.gh_activity_limit(test_gh_activity_limit=50, system_gh_activity_limit=20)
def test_gh_call_budget() -> None:
    """Fail if total gh calls exceed configured budget."""
    gh_audit.emit_report()

    files = sorted(E2E_LOG_DIR.glob("gh-audit-*.json"))
    if not files:
        pytest.fail(f"No GH audit files found in {E2E_LOG_DIR}")

    total_calls = 0
    total_errors = 0
    total_items = 0
    total_bytes = 0
    total_usage_units = 0
    by_command: dict[str, int] = {}
    by_caller: dict[str, int] = {}
    by_scope: dict[str, int] = {}
    by_scope_usage: dict[str, int] = {}

    for path in files:
        data = json.loads(path.read_text())
        total_calls += int(data.get("total_calls", 0))
        total_errors += int(data.get("errors", 0))
        total_items += int(data.get("total_items_returned", 0))
        total_bytes += int(data.get("total_bytes_returned", 0))
        total_usage_units += int(data.get("usage_units", 0))
        for cmd, count in (data.get("by_command") or {}).items():
            by_command[cmd] = by_command.get(cmd, 0) + int(count)
        for caller, count in (data.get("by_caller") or {}).items():
            by_caller[caller] = by_caller.get(caller, 0) + int(count)
        for scope, count in (data.get("by_scope") or {}).items():
            by_scope[scope] = by_scope.get(scope, 0) + int(count)
        for scope, entry in (data.get("by_scope_totals") or {}).items():
            by_scope_usage[scope] = by_scope_usage.get(scope, 0) + int(entry.get("usage_units", 0))

    max_calls = int(os.environ.get("E2E_GH_MAX_CALLS", "2000"))
    max_usage = int(os.environ.get("E2E_GH_MAX_USAGE_UNITS", "0"))
    max_test_calls = int(os.environ.get("E2E_GH_MAX_CALLS_TEST", "0"))
    max_test_usage = int(os.environ.get("E2E_GH_MAX_USAGE_UNITS_TEST", "0"))
    if total_calls > max_calls:
        top_cmds = sorted(by_command.items(), key=lambda kv: kv[1], reverse=True)[:5]
        top_callers = sorted(by_caller.items(), key=lambda kv: kv[1], reverse=True)[:5]
        pytest.fail(
            "GH call budget exceeded: "
            f"total_calls={total_calls} max_calls={max_calls} errors={total_errors} "
            f"items={total_items} bytes={total_bytes} usage_units={total_usage_units} "
            f"top_cmds={top_cmds} top_callers={top_callers}"
        )
    if max_usage and total_usage_units > max_usage:
        top_cmds = sorted(by_command.items(), key=lambda kv: kv[1], reverse=True)[:5]
        pytest.fail(
            "GH usage budget exceeded: "
            f"usage_units={total_usage_units} max_usage={max_usage} "
            f"total_calls={total_calls} items={total_items} bytes={total_bytes} "
            f"top_cmds={top_cmds}"
        )
    if max_test_calls or max_test_usage:
        startup_calls = by_scope.get("startup", 0)
        periodic_calls = by_scope.get("periodic", 0)
        charged_calls = total_calls - startup_calls - periodic_calls
        charged_usage = total_usage_units - by_scope_usage.get("startup", 0) - by_scope_usage.get("periodic", 0)
        if max_test_calls and charged_calls > max_test_calls:
            pytest.fail(
                "GH test budget exceeded: "
                f"charged_calls={charged_calls} max_test_calls={max_test_calls} "
                f"startup_calls={startup_calls} periodic_calls={periodic_calls}"
            )
        if max_test_usage and charged_usage > max_test_usage:
            pytest.fail(
                "GH test usage budget exceeded: "
                f"charged_usage={charged_usage} max_test_usage={max_test_usage}"
            )
