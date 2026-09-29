"""``improver validate``: the budgeted-validation exit codes (#7490)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.cli_tools import improver
from tests.unit.improver_support import build_improver_data, example


def test_a_valid_findings_file_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    build_improver_data(tmp_path)
    (tmp_path / improver.FINDINGS_FILE).write_text(json.dumps(example("exam_case")))

    assert improver.main(["validate", "--run-dir", str(tmp_path)]) == improver.EXIT_OK
    assert "valid: 1 finding(s)" in capsys.readouterr().out


def test_a_rejected_findings_file_exits_one_naming_each_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build_improver_data(tmp_path)
    doc = example("exam_case")
    doc["findings"][0]["reproduction"]["fails_on"] = "HEAD"
    (tmp_path / improver.FINDINGS_FILE).write_text(json.dumps(doc))

    assert improver.main(["validate", "--run-dir", str(tmp_path)]) == improver.EXIT_REJECTED
    assert "[reproduction_fails_on_engine_commit]" in capsys.readouterr().out


def test_no_findings_file_is_a_rejection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    build_improver_data(tmp_path)

    assert improver.main(["validate", "--run-dir", str(tmp_path)]) == improver.EXIT_REJECTED
    assert "wrote no improver-findings.json" in capsys.readouterr().out


def test_staging_an_engine_without_a_start_record_exits_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Host:
        def list_open_issue_labels_complete(self):  # pragma: no cover - never reached
            raise AssertionError

    monkeypatch.setattr(improver, "create_repository_host", lambda repo: Host())
    state = tmp_path / "state"
    state.mkdir()

    code = improver.main([
        "stage", "--state-dir", str(state), "--audited-repo", "o/r", "--outputs-repo", "o/r",
        "--run-dir", str(tmp_path / "run"), "--no-github",
    ])

    assert code == improver.EXIT_UNAVAILABLE
