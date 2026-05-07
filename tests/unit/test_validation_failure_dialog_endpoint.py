"""Endpoint-level regression for the validation-failure dialog route.

This test exists because the prior PR (#6203) added structured
`junit_cases` to the dialog payload contract but did not thread the
orchestrator's `validation.junit_xml_paths` config through the
endpoint code path. The result: the dialog endpoint
(`GET /api/dialog/validation-failure/{issue}`) returned
`junit_cases: []` for *every* request, even on repos that emit JUnit
XML.

The unit-level test in `tests/integration/test_validation_failure_view_model.py`
bypassed this by passing `junit_xml_paths` directly to
`load_validation_failure_summary()`. It proved the serialization +
dialog-builder layers worked, but missed the config-threading layer
in the actual route. This test exercises the route.

If this test fails because `junit_cases` is empty, the fix is in
`web_session_routes._manifest_response` and/or
`web_issue_detail_routes._current_run_validation_diagnostic` —
not in the test or the dialog builder.
"""

from __future__ import annotations

import json
from pathlib import Path

# Reuse the test scaffolding (mock orchestrator, app fixture, set_orchestrator
# helper) so we get the same FastAPI app + dependency wiring everything else
# in this directory uses.
from tests.unit import test_web as _support  # noqa: F401
from tests.unit.test_web import *  # noqa: F401, F403  -- TestClient, app, etc.

from fastapi.testclient import TestClient

from issue_orchestrator.domain.models import SessionHistoryEntry
from issue_orchestrator.entrypoints import web
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput


# Realistic JUnit XML covering one passing case + two failing cases.
# Mirrors what pytest --junitxml emits.
_JUNIT_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="3" failures="2" errors="0" skipped="0" time="0.30">
    <testcase classname="tests.unit.test_one" name="test_pass" time="0.001"/>
    <testcase classname="tests.unit.test_one" name="test_fails_with_assertion"
              file="tests/unit/test_one.py" line="20" time="0.005">
      <failure message="AssertionError: expected 2, got 1">
def test_fails_with_assertion():
&gt;       assert compute() == 2
E       AssertionError: expected 2, got 1
E       assert 1 == 2
E        +  where 1 = compute()

tests/unit/test_one.py:22: AssertionError
      </failure>
    </testcase>
    <testcase classname="tests.unit.test_two" name="test_fails_with_typeerror"
              file="tests/unit/test_two.py" line="11" time="0.003">
      <failure message="TypeError: 'NoneType' object is not subscriptable">
def test_fails_with_typeerror():
        result = lookup(None)
&gt;       assert result["name"] == "Alice"
E       TypeError: 'NoneType' object is not subscriptable

tests/unit/test_two.py:13: TypeError
      </failure>
    </testcase>
  </testsuite>
</testsuites>
"""


_VALIDATION_RECORD_JSON = json.dumps({
    "schema_version": 1,
    "suite": "publish_gate",
    "head_sha": "deadbeef00",
    "passed": False,
    "exit_code": 2,
    "command": "make validate",
    "started_at": "2026-05-05T00:00:00Z",
    "ended_at": "2026-05-05T00:00:30Z",
    "timed_out": False,
})


_STDOUT = (
    "FAILED tests/unit/test_one.py::test_fails_with_assertion\n"
    "FAILED tests/unit/test_two.py::test_fails_with_typeerror\n"
)


class TestValidationFailureDialogEndpointJUnit:
    """End-to-end: orchestrator config → dialog endpoint → junit_cases."""

    def test_endpoint_returns_junit_cases_when_config_has_junit_xml_paths(
        self, tmp_path: Path
    ) -> None:
        """When `config.validation.junit_xml_paths` is set, the dialog
        endpoint must return populated `junit_cases` with structured
        per-test failure detail.

        This is the production user path: timeline shows a failed
        validation row → user clicks → dialog opens → user reads which
        tests failed and why. The actionable info lives in
        `payload["junit_cases"][i]["failure_details"]`.
        """
        mock_orch = create_mock_orchestrator()  # noqa: F405 (from test_web)
        # The fix: orchestrator config carries the JUnit XML path.
        mock_orch.config.validation.junit_xml_paths = ("junit.xml",)

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-junit-dialog"
        worktree.mkdir(parents=True)

        run = session_output.start_run(worktree, "coding-1", issue_number=4242)
        session_output.update_manifest(
            run.run_dir,
            {
                "validation_status": "failed",
                "validation_reason": "2 unit tests failed",
                "validation_record_path": str(
                    run.run_dir.relative_to(worktree) / "validation-record.json"
                ),
                "validation_stdout": str(
                    run.run_dir.relative_to(worktree) / "validation-stdout.log"
                ),
            },
        )
        (run.run_dir / "validation-record.json").write_text(_VALIDATION_RECORD_JSON)
        (run.run_dir / "validation-stdout.log").write_text(_STDOUT)
        # JUnit XML path is interpreted relative to the worktree.
        (worktree / "junit.xml").write_text(_JUNIT_XML)

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=4242,
                title="Issue 4242",
                agent_type="agent:web",
                status="blocked",
                runtime_minutes=5,
                worktree_path=worktree,
            ),
        ]

        web.set_orchestrator(mock_orch)
        try:
            client = TestClient(web.app)
            response = client.get(
                f"/api/dialog/validation-failure/4242?run_dir={run.run_dir}"
            )
            assert response.status_code == 200, response.text
            payload = response.json()

            # The structured cases must reach the user-facing endpoint.
            junit_cases = payload.get("junit_cases")
            assert junit_cases, (
                "Endpoint returned empty junit_cases. The fix in "
                "web_session_routes._manifest_response (or the issue-detail "
                "route) is not threading orchestrator.config.validation."
                "junit_xml_paths through to load_validation_failure_summary."
            )

            # And each failed case must carry actionable detail — the
            # actual assertion error / traceback the user reads.
            failed = [c for c in junit_cases if c["outcome"] == "failed"]
            assert len(failed) == 2, f"expected 2 failures, got {failed}"
            for case in failed:
                assert case.get("failure_details"), (
                    f"Case {case.get('case_id')!r} reached the dialog "
                    "but its failure_details is empty — the user can see "
                    "the test name but not why it failed."
                )

            # And the easy-to-spot identifiers are present.
            case_ids = {c["case_id"] for c in junit_cases}
            assert any("test_fails_with_assertion" in cid for cid in case_ids)
            assert any("test_fails_with_typeerror" in cid for cid in case_ids)
        finally:
            web.set_orchestrator(None)

    def test_endpoint_returns_passed_run_payload(self, tmp_path: Path) -> None:
        """Passed runs must reach the dialog too — same endpoint, same payload
        shape, ``status`` flips to "passed".

        This is the user-facing win: previously only failed runs got a
        clickable dialog. Now a passing run can be inspected the same way
        (junit table, command, exit code, stdout tail).
        """
        mock_orch = create_mock_orchestrator()  # noqa: F405
        mock_orch.config.validation.junit_xml_paths = ("junit.xml",)

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-passed-dialog"
        worktree.mkdir(parents=True)

        run = session_output.start_run(worktree, "coding-1", issue_number=4244)
        session_output.update_manifest(
            run.run_dir,
            {
                "validation_status": "passed",
                "validation_record_path": str(
                    run.run_dir.relative_to(worktree) / "validation-record.json"
                ),
                "validation_stdout": str(
                    run.run_dir.relative_to(worktree) / "validation-stdout.log"
                ),
            },
        )
        passed_record = json.dumps({
            "schema_version": 1,
            "suite": "publish_gate",
            "head_sha": "cafebabe",
            "passed": True,
            "exit_code": 0,
            "command": "make validate",
            "started_at": "2026-05-07T12:00:00Z",
            "ended_at": "2026-05-07T12:04:30Z",
            "timed_out": False,
        })
        (run.run_dir / "validation-record.json").write_text(passed_record)
        (run.run_dir / "validation-stdout.log").write_text(
            "============= 142 passed in 41.21s =============\n"
        )
        # All-green JUnit (one passing case) so we exercise the JUnit path
        # for passed runs too — operators reviewing a green run still want
        # the structured per-test view.
        (worktree / "junit.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<testsuites>\n'
            '  <testsuite name="pytest" tests="1" failures="0" errors="0" skipped="0">\n'
            '    <testcase classname="tests.unit.test_one" name="test_pass" time="0.001"/>\n'
            '  </testsuite>\n'
            '</testsuites>\n'
        )

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=4244,
                title="Issue 4244",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=5,
                worktree_path=worktree,
            ),
        ]

        web.set_orchestrator(mock_orch)
        try:
            client = TestClient(web.app)
            response = client.get(
                f"/api/dialog/validation-failure/4244?run_dir={run.run_dir}"
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["status"] == "passed"
            assert payload["title"] == "Validation Passed #4244"
            assert payload["exit_code"] == 0
            assert payload["failed_tests"] == []
            # Passed run still surfaces the JUnit table.
            assert payload["junit_cases"]
            assert payload["junit_cases"][0]["outcome"] == "passed"
        finally:
            web.set_orchestrator(None)

    def test_endpoint_returns_empty_junit_cases_when_config_unset(
        self, tmp_path: Path
    ) -> None:
        """Sanity: when no junit_xml_paths configured, junit_cases is
        empty. The dialog still works (failed_tests / stdout_excerpt
        carry the unstructured info); we just don't get the structured
        per-test view.
        """
        mock_orch = create_mock_orchestrator()  # noqa: F405
        mock_orch.config.validation.junit_xml_paths = ()

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-no-junit"
        worktree.mkdir(parents=True)

        run = session_output.start_run(worktree, "coding-1", issue_number=4243)
        session_output.update_manifest(
            run.run_dir,
            {
                "validation_status": "failed",
                "validation_reason": "Validation failed",
                "validation_record_path": str(
                    run.run_dir.relative_to(worktree) / "validation-record.json"
                ),
                "validation_stdout": str(
                    run.run_dir.relative_to(worktree) / "validation-stdout.log"
                ),
            },
        )
        (run.run_dir / "validation-record.json").write_text(_VALIDATION_RECORD_JSON)
        (run.run_dir / "validation-stdout.log").write_text(_STDOUT)

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=4243,
                title="Issue 4243",
                agent_type="agent:web",
                status="blocked",
                runtime_minutes=5,
                worktree_path=worktree,
            ),
        ]

        web.set_orchestrator(mock_orch)
        try:
            client = TestClient(web.app)
            response = client.get(
                f"/api/dialog/validation-failure/4243?run_dir={run.run_dir}"
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["junit_cases"] == []
            # Unstructured fallback still present.
            assert payload["failed_tests"]
        finally:
            web.set_orchestrator(None)
