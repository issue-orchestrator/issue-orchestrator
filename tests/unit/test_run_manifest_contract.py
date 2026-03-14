"""Run manifest contract schema tests."""

from __future__ import annotations

import json
from pathlib import Path

from issue_orchestrator.contracts.run_manifest import (
    is_valid_run_manifest_payload,
    run_manifest_json_schema,
)


def test_run_manifest_schema_is_current() -> None:
    base_dir = Path(__file__).resolve().parents[2]
    schema_path = base_dir / "contracts" / "run-manifest.schema.json"
    expected = run_manifest_json_schema()
    current = json.loads(schema_path.read_text())
    assert current == expected


def test_run_manifest_strict_requires_core_log_artifacts() -> None:
    payload = {
        "session_name": "coding-1",
        "run_id": "20260219-000000Z",
        "run_dir": "/tmp/repo/.issue-orchestrator/sessions/20260219-000000Z__coding-1",
        "artifacts": {
            "terminal_recording": {
                "kind": "terminal_recording",
                "path": "/tmp/run/terminal-recording.jsonl",
            },
        },
    }
    assert is_valid_run_manifest_payload(payload, strict_required_artifacts=True)
    assert not is_valid_run_manifest_payload(
        {
            "session_name": "coding-1",
            "run_id": "20260219-000000Z",
            "run_dir": "/tmp/repo/.issue-orchestrator/sessions/20260219-000000Z__coding-1",
            "artifacts": {},
        },
        strict_required_artifacts=True,
    )
