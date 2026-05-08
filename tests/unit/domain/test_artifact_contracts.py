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
    ChapterSidecarArtifact,
    CoderTurnStarted,
    ExchangeRunId,
    ExistingDirectory,
    ExistingFile,
    ExistingNonEmptyFile,
    IssueNumber,
    PositiveAttemptIndex,
    PositiveRoundIndex,
    PromptArtifact,
    RenderMode,
    ReviewExchangeArtifactScope,
    ReviewExchangeSummaryArtifact,
    ReviewerTurnCompleted,
    ReviewerTurnStarted,
    ReviewResponseArtifact,
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
        attempt_index=PositiveAttemptIndex(1),
        role=AgentRole.REVIEWER,
        provider=AgentProvider("claude-code"),
    )


def test_identity_primitives_reject_invalid_values() -> None:
    with pytest.raises(ArtifactContractViolation, match="IssueNumber.value"):
        IssueNumber(0)

    with pytest.raises(ArtifactContractViolation, match="PositiveRoundIndex.value"):
        PositiveRoundIndex(False)

    with pytest.raises(ArtifactContractViolation, match="PositiveAttemptIndex.value"):
        PositiveAttemptIndex(0)

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


def test_reviewer_turn_started_requires_reviewer_scope_and_artifacts(
    tmp_path: Path,
) -> None:
    scope = _turn_scope(tmp_path)
    chapters_path = _write(tmp_path / "reviewer" / "chapters.json", '{"chapters":[]}')
    started = ReviewerTurnStarted(
        scope=scope,
        prompt=PromptArtifact(
            scope=scope,
            file=ExistingNonEmptyFile(_write(tmp_path / "prompt.md", "prompt")),
        ),
        terminal_recording=TerminalRecordingArtifact(
            scope=scope,
            file=ExistingFile(_write(tmp_path / "terminal-recording.jsonl", "")),
        ),
        chapters=ChapterSidecarArtifact(
            scope=scope,
            file=ExistingFile(chapters_path),
        ),
    )

    assert [ref.artifact_type for ref in started.artifact_refs()] == [
        ArtifactType.PROMPT,
        ArtifactType.TERMINAL_RECORDING,
        ArtifactType.CHAPTER_SIDECAR,
    ]
    assert started.to_manifest_fields()["role"] == "reviewer"


def test_reviewer_turn_started_rejects_coder_scope(tmp_path: Path) -> None:
    coder_scope = AgentTurnArtifactScope(
        issue_number=IssueNumber(360),
        exchange_run_id=ExchangeRunId("20260508-review-360"),
        round_index=PositiveRoundIndex(1),
        attempt_index=PositiveAttemptIndex(1),
        role=AgentRole.CODER,
        provider=AgentProvider("claude-code"),
    )

    with pytest.raises(ArtifactContractViolation, match="must be reviewer"):
        ReviewerTurnStarted(
            scope=coder_scope,
            prompt=PromptArtifact(
                scope=coder_scope,
                file=ExistingNonEmptyFile(_write(tmp_path / "prompt.md", "prompt")),
            ),
            terminal_recording=TerminalRecordingArtifact(
                scope=coder_scope,
                file=ExistingFile(_write(tmp_path / "terminal-recording.jsonl", "")),
            ),
            chapters=ChapterSidecarArtifact(
                scope=coder_scope,
                file=ExistingFile(_write(tmp_path / "chapters.json", "{}")),
            ),
        )


def test_turn_completed_includes_response_artifact(tmp_path: Path) -> None:
    scope = _turn_scope(tmp_path)
    started = ReviewerTurnStarted(
        scope=scope,
        prompt=PromptArtifact(
            scope=scope,
            file=ExistingNonEmptyFile(_write(tmp_path / "prompt.md", "prompt")),
        ),
        terminal_recording=TerminalRecordingArtifact(
            scope=scope,
            file=ExistingFile(_write(tmp_path / "terminal-recording.jsonl", "")),
        ),
        chapters=ChapterSidecarArtifact(
            scope=scope,
            file=ExistingFile(_write(tmp_path / "chapters.json", "{}")),
        ),
    )
    completed = ReviewerTurnCompleted(
        started=started,
        response=ReviewResponseArtifact(
            scope=scope,
            file=ExistingFile(_write(tmp_path / "result.json", "{}")),
        ),
        response_type="ok",
        response_text="Looks good",
    )

    assert completed.to_manifest_fields()["response_type"] == "ok"
    assert [ref.artifact_type for ref in completed.artifact_refs()] == [
        ArtifactType.PROMPT,
        ArtifactType.TERMINAL_RECORDING,
        ArtifactType.CHAPTER_SIDECAR,
        ArtifactType.REVIEW_RESPONSE,
    ]


def test_coder_turn_started_requires_coder_scope(tmp_path: Path) -> None:
    reviewer_scope = _turn_scope(tmp_path)

    with pytest.raises(ArtifactContractViolation, match="must be coder"):
        CoderTurnStarted(
            scope=reviewer_scope,
            prompt=PromptArtifact(
                scope=reviewer_scope,
                file=ExistingNonEmptyFile(_write(tmp_path / "prompt.md", "prompt")),
            ),
            terminal_recording=TerminalRecordingArtifact(
                scope=reviewer_scope,
                file=ExistingFile(_write(tmp_path / "terminal-recording.jsonl", "")),
            ),
            chapters=ChapterSidecarArtifact(
                scope=reviewer_scope,
                file=ExistingFile(_write(tmp_path / "chapters.json", "{}")),
            ),
        )
