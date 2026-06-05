"""PR comment rendering for completed review exchanges."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..domain.review_artifacts import REVIEW_REPORT_ARTIFACT
from ..domain.review_exchange_turn import Role
from ..domain.review_exchange_turn_artifacts import (
    ReviewExchangeTurnResultArtifact,
    iter_scoped_turn_result_artifacts,
)
from ..ports.review_artifact_reader import (
    ReviewArtifactReadCommand,
    ReviewArtifactReader,
)

GITHUB_COMMENT_BODY_LIMIT = 64 * 1024
_TRUNCATION_NOTICE = (
    "\n\n[Review exchange transcript truncated to keep this PR comment under "
    "GitHub's 64 KiB limit. Full per-turn artifacts remain in the review "
    "exchange run directory.]"
)


def build_review_exchange_pr_comment_body(
    *,
    issue_number: int,
    run_dir: Path,
    exchange_dir: Path | None,
    artifacts: list[dict[str, str]],
    review_artifact_reader: ReviewArtifactReader,
    max_chars: int | None = None,
) -> str | None:
    """Return the review-exchange body appended to the PR completion comment."""
    transcript = _review_exchange_transcript_body(
        issue_number=issue_number,
        run_dir=run_dir,
        exchange_dir=exchange_dir,
        review_artifact_reader=review_artifact_reader,
    )
    if transcript:
        return _limit_comment_body(transcript, max_chars=max_chars)
    report_body = _review_report_comment_body(
        issue_number=issue_number,
        run_dir=run_dir,
        artifacts=artifacts,
        review_artifact_reader=review_artifact_reader,
    )
    if report_body:
        return _limit_comment_body(report_body, max_chars=max_chars)
    return None


def _review_exchange_transcript_body(
    *,
    issue_number: int,
    run_dir: Path,
    exchange_dir: Path | None,
    review_artifact_reader: ReviewArtifactReader,
) -> str | None:
    if exchange_dir is None:
        return None
    blocks: list[str] = []
    for artifact in iter_scoped_turn_result_artifacts(
        run_dir=run_dir,
        exchange_dir=exchange_dir,
    ):
        text = _turn_comment_text(
            issue_number=issue_number,
            run_dir=run_dir,
            artifact=artifact,
            review_artifact_reader=review_artifact_reader,
        )
        if not text:
            continue
        heading = _turn_heading(
            round_index=artifact.round_index,
            role=artifact.role,
            attempt_index=artifact.attempt_index,
        )
        blocks.append(f"### {heading}\n\n{text}")
    if not blocks:
        return None
    return "## Review Exchange Transcript\n\n" + "\n\n".join(blocks)


def _limit_comment_body(text: str, *, max_chars: int | None) -> str:
    if max_chars is None or len(text) <= max_chars:
        return text
    if max_chars <= 0:
        return ""
    if max_chars <= len(_TRUNCATION_NOTICE):
        return _TRUNCATION_NOTICE[:max_chars]
    body_budget = max_chars - len(_TRUNCATION_NOTICE)
    return text[:body_budget].rstrip() + _TRUNCATION_NOTICE


def _turn_heading(*, round_index: int, role: Role, attempt_index: int) -> str:
    heading = f"Round {round_index} {role.value.title()}"
    if attempt_index > 1:
        heading += f" Attempt {attempt_index}"
    return heading


def _turn_comment_text(
    *,
    issue_number: int,
    run_dir: Path,
    artifact: ReviewExchangeTurnResultArtifact,
    review_artifact_reader: ReviewArtifactReader,
) -> str | None:
    if artifact.role is Role.REVIEWER:
        report_text = _reviewer_report_text(
            issue_number=issue_number,
            run_dir=run_dir,
            artifact=artifact,
            review_artifact_reader=review_artifact_reader,
        )
        if report_text:
            return report_text
    return _response_text_from_result(artifact.result_path)


def _reviewer_report_text(
    *,
    issue_number: int,
    run_dir: Path,
    artifact: ReviewExchangeTurnResultArtifact,
    review_artifact_reader: ReviewArtifactReader,
) -> str | None:
    report_path = artifact.review_report_path
    if not report_path.exists():
        return None
    try:
        content = review_artifact_reader.read_review_artifact(
            ReviewArtifactReadCommand(
                issue_number=issue_number,
                run_dir=run_dir,
                artifact_path=str(report_path),
                artifact_type=REVIEW_REPORT_ARTIFACT,
            )
        )
    except FileNotFoundError:
        return None
    text = content.content.strip()
    return text or None


def _response_text_from_result(result_path: Path) -> str | None:
    try:
        data: Any = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    response_text = data.get("response_text")
    if not isinstance(response_text, str):
        return None
    text = response_text.strip()
    return text or None


def _review_report_comment_body(
    *,
    issue_number: int,
    run_dir: Path,
    artifacts: list[dict[str, str]],
    review_artifact_reader: ReviewArtifactReader,
) -> str | None:
    for artifact in artifacts:
        if artifact.get("type") != REVIEW_REPORT_ARTIFACT:
            continue
        artifact_path = artifact.get("value")
        if not artifact_path:
            continue
        try:
            content = review_artifact_reader.read_review_artifact(
                ReviewArtifactReadCommand(
                    issue_number=issue_number,
                    run_dir=run_dir,
                    artifact_path=artifact_path,
                    artifact_type=REVIEW_REPORT_ARTIFACT,
                )
            )
        except FileNotFoundError:
            continue
        text = content.content.strip()
        if text:
            return text
    return None
