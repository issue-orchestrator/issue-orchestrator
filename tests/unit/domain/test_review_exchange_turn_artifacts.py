"""Tests for shared review-exchange turn artifact paths."""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.domain.review_artifacts import REVIEW_REPORT_FILENAME
from issue_orchestrator.domain.review_exchange_turn import Role
from issue_orchestrator.domain.review_exchange_turn_artifacts import (
    iter_scoped_turn_result_artifacts,
    parse_turn_result_artifact,
    review_exchange_dir,
    turn_artifact_path,
)


def test_turn_result_artifact_path_round_trips(tmp_path: Path) -> None:
    exchange_dir = tmp_path / "run" / "review-exchange"
    result_path = turn_artifact_path(
        exchange_dir,
        round_index=2,
        role=Role.CODER,
        attempt_index=3,
        suffix="result.json",
        create_dir=True,
    )
    result_path.write_text("{}", encoding="utf-8")

    artifact = parse_turn_result_artifact(result_path)

    assert artifact is not None
    assert artifact.round_index == 2
    assert artifact.role is Role.CODER
    assert artifact.attempt_index == 3
    assert artifact.result_path == result_path
    assert artifact.review_report_path == (
        result_path.parent / f"round-2-coder-attempt-3.{REVIEW_REPORT_FILENAME}"
    )


def test_iter_scoped_turn_result_artifacts_rejects_out_of_run_dir(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    outside_exchange = tmp_path / "outside" / "review-exchange"
    outside_result = turn_artifact_path(
        outside_exchange,
        round_index=1,
        role=Role.REVIEWER,
        attempt_index=1,
        suffix="result.json",
        create_dir=True,
    )
    outside_result.write_text("{}", encoding="utf-8")

    artifacts = iter_scoped_turn_result_artifacts(
        run_dir=run_dir,
        exchange_dir=outside_exchange,
    )

    assert artifacts == ()


def test_iter_scoped_turn_result_artifacts_orders_reviewer_before_coder(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    exchange_dir = review_exchange_dir(run_dir)
    for role in (Role.CODER, Role.REVIEWER):
        path = turn_artifact_path(
            exchange_dir,
            round_index=1,
            role=role,
            attempt_index=1,
            suffix="result.json",
            create_dir=True,
        )
        path.write_text("{}", encoding="utf-8")

    artifacts = iter_scoped_turn_result_artifacts(
        run_dir=run_dir,
        exchange_dir=exchange_dir,
    )

    assert [artifact.role for artifact in artifacts] == [Role.REVIEWER, Role.CODER]
