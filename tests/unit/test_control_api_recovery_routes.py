"""Authenticated HTTP boundary for cold retained-work discovery."""

from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.control_center_recovery_queries import (
    ControlCenterRecoveryQueries,
    UnknownConfiguredRepositoryError,
)
from issue_orchestrator.domain.control_center_recovery import (
    ControlCenterRecoveryRows,
    RecoveryRecordFact,
    RecoveryRowsStatus,
    UnownedRecoveryRecord,
)
from issue_orchestrator.domain.validated_work import (
    RemoteBaselineStatus,
    ValidatedWorkKey,
    ValidatedWorkState,
)
from issue_orchestrator.domain.validated_work_commands import (
    ValidatedWorkAuthoritySnapshot,
)
from issue_orchestrator.entrypoints.control_api import control_app
from issue_orchestrator.entrypoints.control_api_repo_support import (
    ControlApiRepoDependencies,
    get_control_api_repo_dependencies,
)

REPO_KEY = "repo-" + "a" * 64


@pytest.fixture
def recovery_queries(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    queries = MagicMock(spec=ControlCenterRecoveryQueries)
    deps = ControlApiRepoDependencies(
        get_supervisor=MagicMock(),
        get_control_actions=MagicMock(),
        validate_repo_root=MagicMock(),
        get_preferred_repo_root=MagicMock(),
        get_expected_engine_identity_raw=MagicMock(),
        get_recovery_queries=lambda: queries,
    )
    monkeypatch.setitem(
        control_app.dependency_overrides,
        get_control_api_repo_dependencies,
        lambda: deps,
    )
    return queries


@pytest.mark.parametrize(
    "status",
    [
        RecoveryRowsStatus.EMPTY,
        RecoveryRowsStatus.DATABASE_ABSENT,
        RecoveryRowsStatus.UNREADABLE,
        RecoveryRowsStatus.UNSUPPORTED_SCHEMA,
    ],
)
def test_authenticated_recovery_read_returns_strict_discovery_status(
    status: RecoveryRowsStatus,
    auth_enabled_control_client,
    fake_browser_auth,
    recovery_queries: MagicMock,
) -> None:
    recovery_queries.repository.return_value = ControlCenterRecoveryRows(
        REPO_KEY,
        status,
        (),
        (),
        f"{status.value} result",
    )

    unauthenticated = auth_enabled_control_client.get(
        f"/api/control-center/repositories/{REPO_KEY}/validated-work"
    )
    assert unauthenticated.status_code == 401
    recovery_queries.repository.assert_not_called()

    response = auth_enabled_control_client.get(
        f"/api/control-center/repositories/{REPO_KEY}/validated-work",
        params={"repo_root": "/caller/cannot/select/this"},
        headers=fake_browser_auth.bearer_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "repo_key": REPO_KEY,
        "status": status.value,
        "engine_groups": [],
        "unowned_records": [],
        "message": f"{status.value} result",
    }
    recovery_queries.repository.assert_called_once_with(REPO_KEY)


def test_recovery_read_rejects_unknown_and_malformed_repository_keys(
    auth_enabled_control_client,
    fake_browser_auth,
    recovery_queries: MagicMock,
) -> None:
    recovery_queries.repository.side_effect = UnknownConfiguredRepositoryError(REPO_KEY)

    response = auth_enabled_control_client.get(
        f"/api/control-center/repositories/{REPO_KEY}/validated-work",
        headers=fake_browser_auth.bearer_headers(),
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Configured repository was not found"}

    malformed = auth_enabled_control_client.get(
        "/api/control-center/repositories/repo-not-a-digest/validated-work",
        headers=fake_browser_auth.bearer_headers(),
    )
    assert malformed.status_code == 422


def test_recovery_read_serializes_available_unowned_work(
    auth_enabled_control_client,
    fake_browser_auth,
    recovery_queries: MagicMock,
) -> None:
    key = ValidatedWorkKey("owner/repo", 42, "issue-42", "b" * 40)
    authority = ValidatedWorkAuthoritySnapshot(
        record_id=key.record_id,
        evidence_id="evidence-42",
        observation_revision=3,
        validated_head_sha=key.validated_head_sha,
        branch_name=key.branch_name,
        repo_slug=key.repo_slug,
        issue_number=key.issue_number,
        pr_number=None,
        expected_remote_head_sha=None,
        remote_baseline_status=RemoteBaselineStatus.UNOBSERVED,
    )
    row = UnownedRecoveryRecord(
        "unowned",
        RecoveryRecordFact(
            authority,
            ValidatedWorkState.PARKED,
            None,
            "Retained after validation",
            True,
        ),
    )
    recovery_queries.repository.return_value = ControlCenterRecoveryRows(
        REPO_KEY,
        RecoveryRowsStatus.AVAILABLE,
        (),
        (row,),
        "One retained record",
    )

    response = auth_enabled_control_client.get(
        f"/api/control-center/repositories/{REPO_KEY}/validated-work",
        headers=fake_browser_auth.bearer_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "available"
    assert response.json()["engine_groups"] == []
    assert response.json()["unowned_records"][0]["kind"] == "unowned"
    assert (
        response.json()["unowned_records"][0]["work"]["authority"]["record_id"]
        == key.record_id
    )
