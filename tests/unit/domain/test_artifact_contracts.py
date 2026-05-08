from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from issue_orchestrator.domain.artifact_contracts import (
    AgentProvider,
    AgentRole,
    AgentTurnArtifactScope,
    ArtifactContractViolation,
    ArtifactType,
    ExchangeRunId,
    ExistingDirectory,
    ExistingFile,
    ExistingNonEmptyFile,
    IssueNumber,
    PositiveRoundIndex,
    PromptArtifact,
    RenderMode,
    ReviewExchangeArtifactScope,
    ReviewExchangeSummaryArtifact,
    TerminalRecordingArtifact,
    ValidationArtifactScope,
    ValidationResultArtifact,
    artifact_refs,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _turn_scope(tmp_path: Path) -> AgentTurnArtifactScope:
    return AgentTurnArtifactScope(
        issue_number=IssueNumber(360),
        exchange_run_id=ExchangeRunId("20260508-review-360"),
        round_index=PositiveRoundIndex(1),
        role=AgentRole.REVIEWER,
        provider=AgentProvider("claude-code"),
    )


def test_identity_primitives_reject_invalid_values() -> None:
    with pytest.raises(ArtifactContractViolation, match="IssueNumber.value"):
        IssueNumber(0)

    with pytest.raises(ArtifactContractViolation, match="PositiveRoundIndex.value"):
        PositiveRoundIndex(False)

    with pytest.raises(ArtifactContractViolation, match="ExchangeRunId.value"):
        ExchangeRunId("")

    with pytest.raises(ArtifactContractViolation, match="not a path"):
        ExchangeRunId("review/r1")

    with pytest.raises(ArtifactContractViolation, match="AgentProvider.value"):
        AgentProvider(" ")


def test_existing_file_and_directory_evidence_fail_fast(tmp_path: Path) -> None:
    missing_file = tmp_path / "missing.json"
    missing_dir = tmp_path / "missing-dir"

    with pytest.raises(ArtifactContractViolation, match="file not found"):
        ExistingFile(missing_file)

    with pytest.raises(ArtifactContractViolation, match="directory not found"):
        ExistingDirectory(missing_dir)

    with pytest.raises(ArtifactContractViolation, match="file not found"):
        ExistingFile(tmp_path)


def test_non_empty_file_evidence_rejects_empty_file(tmp_path: Path) -> None:
    empty = _write(tmp_path / "terminal-recording.jsonl", "")

    with pytest.raises(ArtifactContractViolation, match="file is empty"):
        ExistingNonEmptyFile(empty)


def test_named_artifact_constructor_requires_all_attributes(tmp_path: Path) -> None:
    recording = ExistingNonEmptyFile(
        _write(tmp_path / "reviewer" / "terminal-recording.jsonl", "prompt\n"),
    )
    artifact_type = cast(Any, TerminalRecordingArtifact)

    with pytest.raises(TypeError, match="required positional argument"):
        artifact_type(file=recording)


def test_terminal_recording_artifact_projects_exact_identity(tmp_path: Path) -> None:
    recording_path = _write(
        tmp_path / "reviewer" / "terminal-recording.jsonl",
        "reviewer prompt\n",
    )
    artifact = TerminalRecordingArtifact(
        scope=_turn_scope(tmp_path),
        file=ExistingNonEmptyFile(recording_path),
    )

    ref = artifact.to_ref()

    assert ref.artifact_type is ArtifactType.TERMINAL_RECORDING
    assert ref.render_mode is RenderMode.TERMINAL_RECORDING
    assert ref.path.path == recording_path
    assert ref.scope.role is AgentRole.REVIEWER
    assert ref.to_timeline_artifact() == {
        "type": "terminal_recording",
        "label": "Terminal Recording",
        "value": str(recording_path),
    }
    assert ref.to_manifest_artifact() == {
        "kind": "terminal_recording",
        "path": str(recording_path),
        "render_mode": "terminal_recording",
    }


def test_artifact_refs_convert_without_run_dir_discovery(tmp_path: Path) -> None:
    scope = _turn_scope(tmp_path)
    prompt = PromptArtifact(
        scope=scope,
        file=ExistingNonEmptyFile(_write(tmp_path / "reviewer" / "prompt.md", "prompt")),
    )
    recording = TerminalRecordingArtifact(
        scope=scope,
        file=ExistingNonEmptyFile(
            _write(tmp_path / "reviewer" / "terminal-recording.jsonl", "recording"),
        ),
    )

    refs = artifact_refs(prompt, recording)

    assert [ref.artifact_type for ref in refs] == [
        ArtifactType.PROMPT,
        ArtifactType.TERMINAL_RECORDING,
    ]
    assert [ref.path.path.name for ref in refs] == [
        "prompt.md",
        "terminal-recording.jsonl",
    ]


def test_review_exchange_and_validation_artifact_scopes_are_explicit(
    tmp_path: Path,
) -> None:
    exchange_dir = tmp_path / "review-exchange"
    exchange_dir.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    summary = ReviewExchangeSummaryArtifact(
        scope=ReviewExchangeArtifactScope(
            issue_number=IssueNumber(360),
            exchange_run_id=ExchangeRunId("20260508-review-360"),
            exchange_dir=ExistingDirectory(exchange_dir),
        ),
        file=ExistingFile(_write(exchange_dir / "summary.json", "{}")),
    )
    validation = ValidationResultArtifact(
        scope=ValidationArtifactScope(
            issue_number=IssueNumber(360),
            run_id=ExchangeRunId("20260508-coding-360"),
            run_dir=ExistingDirectory(run_dir),
        ),
        file=ExistingFile(_write(run_dir / "validation-record.json", "{}")),
    )

    assert summary.to_manifest_artifact()["kind"] == "review_exchange_summary"
    assert validation.to_manifest_artifact()["kind"] == "validation_result"
