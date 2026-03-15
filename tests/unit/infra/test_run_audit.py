from __future__ import annotations

import json
from pathlib import Path

from issue_orchestrator.infra.run_audit import write_run_audit


def test_write_run_audit_summarizes_review_exchange(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    exchange_dir = run_dir / "review-exchange"
    exchange_dir.mkdir(parents=True)

    (run_dir / "manifest.json").write_text(json.dumps({
        "session_name": "issue-4057",
        "run_id": "r1",
        "run_dir": str(run_dir),
        "issue_number": 4057,
        "outcome": "completed",
        "runtime_minutes": 94,
        "started_at": "2026-03-14T22:01:13Z",
        "ended_at": "2026-03-14T23:35:13Z",
        "artifacts": {
            "terminal_recording": {
                "kind": "terminal_recording",
                "path": str(run_dir / "terminal-recording.jsonl"),
            },
        },
    }) + "\n")
    (exchange_dir / "summary.json").write_text(json.dumps({
        "completed_rounds": 2,
        "status": "ok",
        "response_text": "Looks good",
        "timestamp": "2026-03-14T23:40:26Z",
    }))
    (exchange_dir / "round-001.json").write_text(json.dumps({
        "reviewer": {
            "round_index": 1,
            "timestamp": "2026-03-14T22:15:32Z",
            "response_type": "changes_requested",
            "response_text": "Please rework this.",
        },
        "coder": {
            "round_index": 1,
            "timestamp": "2026-03-14T23:02:26Z",
            "response_type": "completed",
            "response_text": "Updated.",
        },
    }))
    (exchange_dir / "round-002.json").write_text(json.dumps({
        "reviewer": {
            "round_index": 2,
            "timestamp": "2026-03-14T23:40:26Z",
            "response_type": "ok",
            "response_text": "Looks good.",
        },
    }))

    result = write_run_audit(
        run_dir,
        issue_labels=["agent:backend", "needs-run-audit"],
        trigger_label="needs-run-audit",
        completion_label="run-audit-complete",
    )

    payload = json.loads(result.path.read_text())
    assert "coder rework after review round 1" in payload["summary"].lower()
    assert payload["dominant_time_bucket"] == "coder_rework"
    assert payload["review_exchange"]["rounds"][0]["coder_rework_minutes"] == 46.9
    assert payload["review_exchange"]["rounds"][1]["reviewer_follow_up_minutes"] == 38.0
