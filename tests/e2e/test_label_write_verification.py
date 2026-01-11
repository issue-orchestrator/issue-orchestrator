"""Live e2e verification for GitHub label writes with cache/ETag paths."""

from __future__ import annotations

import json
import os
import time
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.adapters.github import GitHubAdapter
from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import AddLabelAction, RemoveLabelAction
from issue_orchestrator.infra import gh_audit
from issue_orchestrator.ports import InMemoryEventSink
from issue_orchestrator.testing.support.test_data import create_issue, close_issue
from tests.e2e.conftest import e2e_label
from tests.e2e.fixtures.github_utils import is_github_connection_error


def _wait_for_label_state(
    adapter: GitHubAdapter,
    issue_number: int,
    label: str,
    *,
    present: bool,
    use_cache: bool,
    timeout_s: float = 30.0,
) -> list[str]:
    deadline = time.monotonic() + timeout_s
    last_labels: list[str] = []
    while time.monotonic() < deadline:
        if use_cache:
            labels = adapter.get_issue_labels(issue_number)
        else:
            labels = adapter.get_issue_labels_fresh(issue_number)
        last_labels = labels
        if (label in labels) == present:
            return labels
        time.sleep(1.0)
    state = "present" if present else "absent"
    raise TimeoutError(
        f"Timed out waiting for label '{label}' to be {state} on issue #{issue_number}. "
        f"Last seen labels: {last_labels}"
    )


@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.gh_activity_limit(test_gh_activity_limit=150, system_gh_activity_limit=50)
class TestLabelWriteVerification:
    """Verify label add/remove paths against real GitHub with audit + cache checks."""

    def test_label_write_paths(self, repo_name: str, test_label: str, tmp_path):
        if os.environ.get("E2E_DRY_RUN_PUSH", "1") != "false":
            pytest.skip("Label write verification requires real GitHub writes (E2E_DRY_RUN_PUSH=false)")

        gh_audit.configure(enabled=True, include_events=True, audit_path=str(tmp_path / "gh-audit.json"))
        gh_audit.reset_stats()

        adapter = GitHubAdapter(repo=repo_name)
        event_sink = InMemoryEventSink()
        applier = ActionApplier(
            labels=adapter,
            sessions=MagicMock(),
            events=event_sink,
        )

        base_label = e2e_label(f"{test_label}-base")
        added_label = e2e_label(f"{test_label}-add")
        replaced_label = e2e_label(f"{test_label}-replaced")

        issue_number = None
        try:
            issue_number = create_issue(
                repo_name,
                f"[E2E] Label write verification ({test_label})",
                [base_label],
                body="Verifies add/remove label behavior with cache + audit trails.",
            )

            # Prime the cache/ETag path with initial read.
            adapter.get_issue_labels(issue_number)

            add_action = AddLabelAction(
                issue_number=issue_number,
                label=added_label,
                reason="e2e add label verification",
            )
            add_result = applier.apply(add_action)
            assert add_result.success

            _wait_for_label_state(adapter, issue_number, added_label, present=True, use_cache=False)
            _wait_for_label_state(adapter, issue_number, added_label, present=True, use_cache=True)

            remove_action = RemoveLabelAction(
                issue_number=issue_number,
                label=added_label,
                reason="e2e remove label verification",
            )
            remove_result = applier.apply(remove_action)
            assert remove_result.success

            _wait_for_label_state(adapter, issue_number, added_label, present=False, use_cache=False)
            _wait_for_label_state(adapter, issue_number, added_label, present=False, use_cache=True)

            replace_action = AddLabelAction(
                issue_number=issue_number,
                label=replaced_label,
                reason="e2e change label verification",
            )
            replace_result = applier.apply(replace_action)
            assert replace_result.success

            drop_action = RemoveLabelAction(
                issue_number=issue_number,
                label=base_label,
                reason="e2e change label verification (drop base)",
            )
            drop_result = applier.apply(drop_action)
            assert drop_result.success

            _wait_for_label_state(adapter, issue_number, replaced_label, present=True, use_cache=False)
            _wait_for_label_state(adapter, issue_number, replaced_label, present=True, use_cache=True)
            _wait_for_label_state(adapter, issue_number, base_label, present=False, use_cache=False)
            _wait_for_label_state(adapter, issue_number, base_label, present=False, use_cache=True)

            event_names = [event.name for event in event_sink.events]
            assert "action.start" in event_names
            assert "action.end" in event_names

            report_path = gh_audit.emit_report()
            assert report_path is not None
            report = json.loads((tmp_path / "gh-audit.json").read_text())
            reason_totals = report.get("by_reason_totals", {})
            assert reason_totals.get("gh_write", {}).get("calls", 0) >= 3
            assert reason_totals.get("gh_read", {}).get("calls", 0) >= 1

        except RuntimeError as exc:
            if is_github_connection_error(str(exc)):
                pytest.skip("GitHub API not reachable for live e2e tests")
            raise
        finally:
            if issue_number is not None:
                close_issue(repo_name, issue_number, comment="E2E cleanup: label write verification")
            gh_audit.configure(enabled=False)
