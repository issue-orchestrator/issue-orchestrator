"""Lifetime-pinned Repository Engine stop adapter."""

from __future__ import annotations

import errno
import signal
import sys
from pathlib import Path

import pytest

from issue_orchestrator.adapters.repository_engine_incarnation import (
    LinuxPidfdIncarnationStop,
)
from issue_orchestrator.domain.repository_engine_lifecycle import (
    EngineIdentity,
    EngineStopAvailability,
    StopEngineCommand,
    StopEngineStatus,
)
from issue_orchestrator.domain.validated_work_claim import ProcessIdentity
from issue_orchestrator.infra.repo_lock import LockInfo
from issue_orchestrator.infra.linux_pidfd import LinuxPidfdRuntime


STARTED = "linux-proc-v1:boot-a:12345"


def _engine(
    repo: Path, *, pid: int = 123, instance: str | None = "engine-a"
) -> EngineIdentity:
    process = ProcessIdentity("local", pid, STARTED, instance)
    return EngineIdentity(str(repo), instance, "local", instance or "default", process)


def _advertisement(engine: EngineIdentity) -> LockInfo:
    return LockInfo(
        repo_root=engine.repo_root,
        pid=engine.process.pid,
        started_at="advertisement-time-is-not-process-identity",
        http_port=19080,
        state_dir=str(Path(engine.repo_root) / ".issue-orchestrator/state"),
        instance_id=engine.instance_id,
    )


class PidfdRuntime:
    available = True
    opened: list[int]
    sent: list[tuple[int, signal.Signals]]
    waits: list[float]
    closed: list[int]
    wait_results: list[bool]
    incarnation = STARTED
    open_error: OSError | None = None
    start_error: OSError | None = None
    send_error: OSError | None = None

    def __init__(self, *wait_results: bool) -> None:
        self.opened = []
        self.sent = []
        self.waits = []
        self.closed = []
        self.wait_results = list(wait_results)

    def supported(self) -> bool:
        return self.available

    def open(self, pid: int) -> int:
        self.opened.append(pid)
        if self.open_error is not None:
            raise self.open_error
        return 77

    def process_incarnation(self, pid: int) -> str:  # noqa: ARG002
        if self.start_error is not None:
            raise self.start_error
        return self.incarnation

    def send(self, handle: int, sig: signal.Signals) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((handle, sig))

    def wait_for_exit(self, handle: int, timeout_seconds: float) -> bool:  # noqa: ARG002
        self.waits.append(timeout_seconds)
        return self.wait_results.pop(0)

    def close(self, handle: int) -> None:
        self.closed.append(handle)


def _adapter(
    engine: EngineIdentity,
    runtime: PidfdRuntime,
    advertisement: LockInfo | None,
) -> LinuxPidfdIncarnationStop:
    return LinuxPidfdIncarnationStop(
        runtime,
        read_advertisement=lambda root, instance: (
            advertisement
            if (root, instance)
            == (Path(engine.repo_root).resolve(), engine.instance_id)
            else None
        ),
    )


def test_graceful_stop_signals_only_the_pinned_handle() -> None:
    repo = Path("/repo")
    engine = _engine(repo)
    runtime = PidfdRuntime(True)
    adapter = _adapter(engine, runtime, _advertisement(engine))
    command = StopEngineCommand(engine, "operator", "reason", 7.0, True)

    outcome = adapter.stop_expected(command)

    assert outcome.status is StopEngineStatus.STOPPED
    assert runtime.opened == [123]
    assert runtime.sent == [(77, signal.SIGTERM)]
    assert runtime.waits == [7.0]
    assert runtime.closed == [77]


def test_timeout_forces_the_same_pinned_handle() -> None:
    engine = _engine(Path("/repo"))
    runtime = PidfdRuntime(False, True)
    adapter = _adapter(engine, runtime, _advertisement(engine))

    outcome = adapter.stop_expected(
        StopEngineCommand(engine, "operator", "reason", 0.25, True)
    )

    assert outcome.status is StopEngineStatus.STOPPED
    assert runtime.sent == [(77, signal.SIGTERM), (77, signal.SIGKILL)]
    assert runtime.waits == [0.25, 10.0]
    assert runtime.closed == [77]


def test_graceful_only_timeout_reports_failure_without_force() -> None:
    engine = _engine(Path("/repo"))
    runtime = PidfdRuntime(False)
    adapter = _adapter(engine, runtime, _advertisement(engine))

    outcome = adapter.stop_expected(
        StopEngineCommand(engine, "operator", "reason", 0.25, False)
    )

    assert outcome.status is StopEngineStatus.FAILED
    assert runtime.sent == [(77, signal.SIGTERM)]
    assert runtime.waits == [0.25]
    assert runtime.closed == [77]


def test_replacement_advertisement_refuses_before_open_or_signal() -> None:
    engine = _engine(Path("/repo"))
    replacement = _advertisement(_engine(Path("/repo"), pid=456))
    runtime = PidfdRuntime()
    adapter = _adapter(engine, runtime, replacement)

    outcome = adapter.stop_expected(StopEngineCommand(engine, "operator", "reason"))

    assert outcome.status is StopEngineStatus.TARGET_CHANGED
    assert runtime.opened == []
    assert runtime.sent == []


@pytest.mark.parametrize(
    "advertisement",
    [
        LockInfo("/other", 123, "ad", 1, "/state", instance_id="engine-a"),
        LockInfo("/repo", 123, "ad", 1, "/state", instance_id=None),
    ],
)
def test_repository_and_instance_are_part_of_advertised_identity(
    advertisement: LockInfo,
) -> None:
    engine = _engine(Path("/repo"))
    runtime = PidfdRuntime()
    adapter = LinuxPidfdIncarnationStop(
        runtime, read_advertisement=lambda _root, _instance: advertisement
    )

    outcome = adapter.stop_expected(StopEngineCommand(engine, "operator", "reason"))

    assert outcome.status is StopEngineStatus.TARGET_CHANGED
    assert runtime.sent == []


def test_kernel_start_identity_mismatch_refuses_the_pinned_pid() -> None:
    engine = _engine(Path("/repo"))
    runtime = PidfdRuntime()
    runtime.incarnation = "linux-proc-v1:boot-a:12346"
    adapter = _adapter(engine, runtime, _advertisement(engine))

    outcome = adapter.stop_expected(StopEngineCommand(engine, "operator", "reason"))

    assert outcome.status is StopEngineStatus.TARGET_CHANGED
    assert runtime.sent == []
    assert runtime.closed == [77]


def test_same_second_pid_reuse_cannot_authorize_a_signal() -> None:
    engine = _engine(Path("/repo"))
    runtime = PidfdRuntime()
    runtime.incarnation = "linux-proc-v1:boot-a:12346"
    adapter = _adapter(engine, runtime, _advertisement(engine))

    outcome = adapter.stop_expected(StopEngineCommand(engine, "operator", "reason"))

    assert outcome.status is StopEngineStatus.TARGET_CHANGED
    assert runtime.sent == []
    assert runtime.closed == [77]


def test_historical_second_resolution_identity_is_never_targetable() -> None:
    historical_process = ProcessIdentity(
        "local", 123, "2026-09-08T00:00:00", "engine-a"
    )
    historical = EngineIdentity(
        "/repo", "engine-a", "local", "engine-a", historical_process
    )
    runtime = PidfdRuntime()
    adapter = _adapter(historical, runtime, _advertisement(historical))

    assert (
        adapter.stop_availability(historical)
        is EngineStopAvailability.EXACT_TARGET_UNAVAILABLE
    )
    outcome = adapter.stop_expected(StopEngineCommand(historical, "operator", "reason"))
    assert outcome.status is StopEngineStatus.FAILED
    assert runtime.opened == []


class ProbeRuntime(LinuxPidfdRuntime):
    def __init__(
        self,
        open_error: OSError | None = None,
        send_error: OSError | None = None,
    ) -> None:
        self.open_error = open_error
        self.send_error = send_error
        self.closed: list[int] = []
        self.sent: list[tuple[int, int | signal.Signals]] = []

    def process_incarnation(self, pid: int) -> str:  # noqa: ARG002
        return STARTED

    def open(self, pid: int) -> int:  # noqa: ARG002
        if self.open_error is not None:
            raise self.open_error
        return 91

    def send(self, handle: int, sig: int | signal.Signals) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((handle, sig))

    def close(self, handle: int) -> None:
        self.closed.append(handle)


def test_capability_probe_refuses_unsupported_pidfd_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("issue_orchestrator.infra.linux_pidfd.sys.platform", "linux")
    runtime = ProbeRuntime(open_error=OSError(errno.ENOSYS, "unsupported"))

    assert runtime.supported() is False
    assert runtime.sent == []
    assert runtime.closed == []


def test_capability_probe_closes_handle_when_signal_zero_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("issue_orchestrator.infra.linux_pidfd.sys.platform", "linux")
    runtime = ProbeRuntime(send_error=PermissionError(errno.EPERM, "denied"))

    assert runtime.supported() is False
    assert runtime.sent == []
    assert runtime.closed == [91]


def test_capability_probe_uses_signal_zero_and_closes_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("issue_orchestrator.infra.linux_pidfd.sys.platform", "linux")
    runtime = ProbeRuntime()

    assert runtime.supported() is True
    assert runtime.sent == [(91, 0)]
    assert runtime.closed == [91]


def test_already_gone_and_capability_failure_are_distinct() -> None:
    engine = _engine(Path("/repo"))
    gone = PidfdRuntime()
    gone.open_error = ProcessLookupError()
    gone_outcome = _adapter(engine, gone, None).stop_expected(
        StopEngineCommand(engine, "operator", "reason")
    )
    assert gone_outcome.status is StopEngineStatus.STOPPED

    failed = PidfdRuntime()
    failed.open_error = PermissionError("denied")
    failed_outcome = _adapter(engine, failed, _advertisement(engine)).stop_expected(
        StopEngineCommand(engine, "operator", "reason")
    )
    assert failed_outcome.status is StopEngineStatus.FAILED
    assert failed.sent == []


def test_exit_after_handle_open_is_observed_as_stopped() -> None:
    engine = _engine(Path("/repo"))
    runtime = PidfdRuntime(True)
    runtime.start_error = FileNotFoundError("process exited")
    adapter = _adapter(engine, runtime, _advertisement(engine))

    outcome = adapter.stop_expected(StopEngineCommand(engine, "operator", "reason"))

    assert outcome.status is StopEngineStatus.STOPPED
    assert runtime.waits == [0]
    assert runtime.sent == []
    assert runtime.closed == [77]


def test_unreadable_advertisement_fails_before_process_effect() -> None:
    engine = _engine(Path("/repo"))
    runtime = PidfdRuntime()
    adapter = LinuxPidfdIncarnationStop(
        runtime,
        read_advertisement=lambda _root, _instance: (_ for _ in ()).throw(
            OSError("unreadable")
        ),
    )

    outcome = adapter.stop_expected(StopEngineCommand(engine, "operator", "reason"))

    assert outcome.status is StopEngineStatus.FAILED
    assert runtime.opened == []
    assert runtime.sent == []


def test_unsupported_platform_has_no_fallback_effect() -> None:
    engine = _engine(Path("/repo"))
    runtime = PidfdRuntime()
    runtime.available = False
    adapter = _adapter(engine, runtime, _advertisement(engine))

    assert (
        adapter.stop_availability(engine)
        is EngineStopAvailability.EXACT_TARGET_UNAVAILABLE
    )
    outcome = adapter.stop_expected(StopEngineCommand(engine, "operator", "reason"))

    assert outcome.status is StopEngineStatus.FAILED
    assert runtime.opened == []
    assert runtime.sent == []


@pytest.mark.skipif(sys.platform == "linux", reason="non-Linux capability contract")
def test_default_non_linux_runtime_reports_exact_target_unavailable() -> None:
    engine = _engine(Path("/repo"))

    assert (
        LinuxPidfdIncarnationStop().stop_availability(engine)
        is EngineStopAvailability.EXACT_TARGET_UNAVAILABLE
    )


def test_adapter_source_contains_no_legacy_stop_escape_hatch() -> None:
    source = Path(
        "src/issue_orchestrator/adapters/repository_engine_incarnation.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "stop_by_port",
        "stop_tracked_instance",
        "stop_all_instances",
        "_request_graceful_shutdown",
        "killpg",
        "release_lock",
        "urlopen",
    ):
        assert forbidden not in source
