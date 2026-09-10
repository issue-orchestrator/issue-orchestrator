"""Production adapter contracts for cold Control Center recovery reads."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from issue_orchestrator.adapters.configured_repository_registry import (
    RegisteredConfiguredRepositoryRegistry,
    configured_repository_key,
)
from issue_orchestrator.adapters.recovery_engine_presentation import (
    SupervisorRecoveryEnginePresentation,
)
from issue_orchestrator.domain.control_center_recovery import (
    RecoveryEnginePresentation,
)
from issue_orchestrator.domain.repository_engine_lifecycle import EngineIdentity
from issue_orchestrator.domain.validated_work_claim import ProcessIdentity
from issue_orchestrator.ports.repository_engine_supervisor import (
    MultiInstanceStatus,
    SupervisorOps,
    SupervisorStatus,
)

OWNER_INCARNATION = "linux-proc-v1:boot-id:123"


def _engine(
    repo: Path,
    *,
    host: str = "local",
    incarnation: str = OWNER_INCARNATION,
) -> EngineIdentity:
    process = ProcessIdentity(host, 123, incarnation, "worker-a")
    return EngineIdentity(str(repo), "worker-a", host, "worker-a", process)


def test_configured_repository_registry_resolves_only_opaque_registered_key(
    tmp_path: Path,
) -> None:
    first = SimpleNamespace(
        path=str(tmp_path / "first"),
        selected_config="default.yaml",
        selected_mode="default",
    )
    second = SimpleNamespace(
        path=str(tmp_path / "second"),
        selected_config="custom.yaml",
        selected_mode="staging",
    )
    slug = MagicMock(side_effect=lambda repo: f"owner/{Path(repo.path).name}")
    registry = RegisteredConfiguredRepositoryRegistry(
        repositories=lambda: (first, second),
        repo_slug=slug,
    )
    key = configured_repository_key(second.path)

    assert key == configured_repository_key(str(Path(second.path) / ".." / "second"))
    assert registry.resolve("/caller/chosen/root") is None
    assert registry.resolve(configured_repository_key(tmp_path / "absent")) is None
    assert registry.resolve(key) == registry.resolve(key)
    resolved = registry.resolve(key)
    assert resolved is not None
    assert resolved.repo_key == key
    assert resolved.repo_root == str(Path(second.path).resolve())
    assert resolved.repo_slug == "owner/second"
    assert all(call.args[0] is second for call in slug.call_args_list)


def test_engine_presentation_distinguishes_exact_missing_and_replaced(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    supervisor = MagicMock(spec=SupervisorOps)
    process_incarnation = MagicMock(return_value=OWNER_INCARNATION)
    reader = SupervisorRecoveryEnginePresentation(
        supervisor,
        local_host="local",
        process_incarnation=process_incarnation,
    )

    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        str(tmp_path),
        [
            SupervisorStatus(
                state="running",
                pid=engine.process.pid,
                started_at="2026-09-08T20:00:00+00:00",
                instance_id=engine.instance_id,
            )
        ],
    )
    assert (
        reader.presentation_for(engine).presentation
        is RecoveryEnginePresentation.OBSERVED
    )
    process_incarnation.assert_called_once_with(engine.process.pid)

    supervisor.status_all_instances.return_value = MultiInstanceStatus(str(tmp_path))
    assert (
        reader.presentation_for(engine).presentation
        is RecoveryEnginePresentation.MISSING
    )

    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        str(tmp_path),
        [
            SupervisorStatus(
                state="running",
                pid=999,
                started_at="2026-09-08T21:00:00+00:00",
                instance_id=engine.instance_id,
            )
        ],
    )
    assert (
        reader.presentation_for(engine).presentation
        is RecoveryEnginePresentation.REPLACED
    )


def test_engine_presentation_never_guesses_legacy_or_unreadable_incarnations(
    tmp_path: Path,
) -> None:
    supervisor = MagicMock(spec=SupervisorOps)
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        str(tmp_path),
        [
            SupervisorStatus(
                state="running",
                pid=123,
                started_at="advertised-at",
                instance_id="worker-a",
            )
        ],
    )
    process_incarnation = MagicMock(side_effect=OSError("proc unavailable"))
    reader = SupervisorRecoveryEnginePresentation(
        supervisor,
        local_host="local",
        process_incarnation=process_incarnation,
    )

    legacy = reader.presentation_for(
        _engine(tmp_path, incarnation="2026-09-08T20:00:00")
    )
    assert legacy.presentation is RecoveryEnginePresentation.UNKNOWN
    process_incarnation.assert_not_called()

    exact = reader.presentation_for(_engine(tmp_path))
    assert exact.presentation is RecoveryEnginePresentation.UNKNOWN
    assert "proc unavailable" in exact.message


def test_engine_presentation_reports_remote_and_unreadable_without_rebinding(
    tmp_path: Path,
) -> None:
    supervisor = MagicMock(spec=SupervisorOps)
    reader = SupervisorRecoveryEnginePresentation(supervisor, local_host="local")
    remote = reader.presentation_for(_engine(tmp_path, host="remote"))
    assert remote.presentation is RecoveryEnginePresentation.UNKNOWN
    assert "remote host" in remote.message
    supervisor.status_all_instances.assert_not_called()

    supervisor.status_all_instances.side_effect = OSError("unreadable lock")
    unknown = reader.presentation_for(_engine(tmp_path))
    assert unknown.presentation is RecoveryEnginePresentation.UNKNOWN
    assert "unreadable lock" in unknown.message
