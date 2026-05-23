"""Unit coverage for the in-memory persistent exchange pair registry.

The registry's contract is the abstraction that PR B2 (lifecycle
persistence — survive across exchanges) and PR B3 (diagnostics) build
on. Locking it down here means the future PRs can change behavior
inside the registry without re-arguing the boundary.

These tests use stand-in subprocess pairs (``_make_pair``) rather than
real PTY-attached agents — the registry only cares about
``proc.poll()`` and ``close_persistent_session``, both of which the
fake exposes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from issue_orchestrator.execution.persistent_exchange_pair_registry_inmemory import (
    InMemoryPersistentExchangePairRegistry,
    PersistentExchangePair,
)


class _FakeProc:
    def __init__(self, *, pid: int) -> None:
        self.pid = pid
        self._return_code: int | None = None

    def poll(self) -> int | None:
        return self._return_code

    def kill(self) -> None:  # pragma: no cover — exercised via close patch
        self._return_code = -9

    def terminate(self) -> None:  # pragma: no cover — same
        self._return_code = -15

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return self._return_code if self._return_code is not None else 0

    def die(self) -> None:
        """Simulate the subprocess exiting on its own."""
        self._return_code = 0


class _FakeSession:
    def __init__(self, *, pid: int) -> None:
        self.proc = _FakeProc(pid=pid)
        self.master_fd = -1
        self.closed = False
        self.log_writer = None

    @property
    def is_live(self) -> bool:
        return not self.closed and self.proc.poll() is None


def _make_pair(
    *,
    issue_key: int = 42,
    coder_pid: int = 1001,
    reviewer_pid: int = 1002,
    reviewer_worktree: Path | None = None,
    created_at: float = 100.0,
) -> PersistentExchangePair:
    base = Path(f"/tmp/fake-pair-{issue_key}")
    return PersistentExchangePair(
        coder_session=_FakeSession(pid=coder_pid),
        reviewer_session=_FakeSession(pid=reviewer_pid),
        reviewer_worktree_path=reviewer_worktree or Path("/tmp/fake-reviewer-wt"),
        issue_key=issue_key,
        created_at=created_at,
        coder_response_path=base / "coder/review-response.json",
        reviewer_response_path=base / "reviewer/review-response.json",
        reviewer_report_path=base / "reviewer/review-report.md",
        coder_recording_path=base / "coder/terminal-recording.jsonl",
        reviewer_recording_path=base / "reviewer/terminal-recording.jsonl",
        coder_completion_path=base / "coder/completion-coder.json",
        validation_record_path=base / "validation-record.json",
    )


@pytest.fixture
def patched_close(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace ``close_persistent_session`` with a tracker.

    The registry's tear-down calls ``close_persistent_session`` on
    each session. We don't want that to actually fight with a real
    PTY, so the test substitutes a recorder.
    """
    closed: list[Any] = []

    def _close(session, **_kwargs):  # noqa: ANN001, ANN202
        closed.append(session)
        session.closed = True
        return 0

    from issue_orchestrator.execution import (
        persistent_exchange_pair_registry_inmemory as mod,
    )
    monkeypatch.setattr(mod, "close_persistent_session", _close)
    return closed


class TestAcquireAndRelease:
    def test_first_acquire_calls_spawn_and_caches_pair(
        self, patched_close: list[Any],
    ) -> None:
        registry = InMemoryPersistentExchangePairRegistry()
        spawn_calls = {"n": 0}

        def _spawn() -> PersistentExchangePair:
            spawn_calls["n"] += 1
            return _make_pair()

        pair = registry.acquire(issue_key=42, spawn=_spawn)
        assert spawn_calls["n"] == 1
        from issue_orchestrator.execution.persistent_exchange_pair_registry_inmemory import (
            _pair_is_alive,  # noqa: PLC2701 — adapter-internal helper under test
        )
        assert _pair_is_alive(pair)

    def test_second_acquire_for_same_key_returns_cached_pair_without_spawn(
        self, patched_close: list[Any],
    ) -> None:
        """The whole point of the registry. B1 always releases between
        exchanges so the cache never hits in production today; this
        test pins the cache-hit semantics that B2 starts to rely on.
        """
        registry = InMemoryPersistentExchangePairRegistry()
        spawn_calls = {"n": 0}
        cached_pair = _make_pair()

        def _spawn() -> PersistentExchangePair:
            spawn_calls["n"] += 1
            return cached_pair

        first = registry.acquire(issue_key=42, spawn=_spawn)
        second = registry.acquire(
            issue_key=42, spawn=lambda: _make_pair(coder_pid=9999),
        )

        assert first is second is cached_pair
        assert spawn_calls["n"] == 1, "second acquire must not call spawn"

    def test_release_evicts_pair_and_calls_close_on_both_sessions(
        self, patched_close: list[Any],
    ) -> None:
        registry = InMemoryPersistentExchangePairRegistry()
        pair = _make_pair()
        registry.acquire(issue_key=42, spawn=lambda: pair)

        registry.release(42, reason="exchange-complete")

        assert {s.proc.pid for s in patched_close} == {1001, 1002}
        # And the cache is empty so a subsequent acquire respawns.
        spawn_calls = {"n": 0}
        registry.acquire(
            issue_key=42,
            spawn=lambda: (spawn_calls.__setitem__("n", spawn_calls["n"] + 1)
                           or _make_pair()),
        )
        assert spawn_calls["n"] == 1

    def test_release_of_absent_key_is_noop(
        self, patched_close: list[Any],
    ) -> None:
        """Idempotent release lets callers put release in a finally
        block without tracking whether acquire even succeeded."""
        registry = InMemoryPersistentExchangePairRegistry()
        registry.release(42, reason="exchange-complete")  # nothing cached
        assert patched_close == []

    def test_dead_pair_on_acquire_is_evicted_and_respawned(
        self, patched_close: list[Any],
    ) -> None:
        """A subprocess that died between exchanges (kernel oom, agent
        crash, segfault) must not be handed back to the next caller —
        the registry must evict it and spawn a fresh pair."""
        registry = InMemoryPersistentExchangePairRegistry()
        original = _make_pair(coder_pid=1001, reviewer_pid=1002)
        registry.acquire(issue_key=42, spawn=lambda: original)

        # Simulate the coder dying between exchanges.
        original.coder_session.proc.die()

        replacement = _make_pair(coder_pid=2001, reviewer_pid=2002)
        returned = registry.acquire(issue_key=42, spawn=lambda: replacement)

        # Old pair was closed, new one spawned and cached.
        assert returned is replacement
        assert any(s.proc.pid == 1001 for s in patched_close), (
            "dead coder must have been closed during eviction"
        )

    def test_closed_pair_on_acquire_is_evicted_and_respawned(
        self, patched_close: list[Any],
    ) -> None:
        """A closed session cannot be reused even if the subprocess still polls live."""
        registry = InMemoryPersistentExchangePairRegistry()
        original = _make_pair(coder_pid=1001, reviewer_pid=1002)
        registry.acquire(issue_key=42, spawn=lambda: original)

        original.coder_session.closed = True

        replacement = _make_pair(coder_pid=2001, reviewer_pid=2002)
        returned = registry.acquire(issue_key=42, spawn=lambda: replacement)

        assert returned is replacement
        assert any(s.proc.pid == 1001 for s in patched_close), (
            "closed coder session must have been closed during eviction"
        )

    def test_shutdown_all_releases_every_cached_pair(
        self, patched_close: list[Any],
    ) -> None:
        registry = InMemoryPersistentExchangePairRegistry()
        registry.acquire(
            issue_key=10, spawn=lambda: _make_pair(coder_pid=10, reviewer_pid=11),
        )
        registry.acquire(
            issue_key=20, spawn=lambda: _make_pair(coder_pid=20, reviewer_pid=21),
        )
        registry.acquire(
            issue_key=30, spawn=lambda: _make_pair(coder_pid=30, reviewer_pid=31),
        )

        registry.shutdown_all(reason="orchestrator-shutdown")

        assert len(patched_close) == 6  # 3 pairs × 2 sessions each


class TestOnReleaseHook:
    def test_on_release_invoked_with_pair_and_reason(
        self, patched_close: list[Any],
    ) -> None:
        """B2 will move reviewer-worktree removal into the on_release
        hook so the worktree's lifetime matches the pair's. Pin the
        contract here so that move is mechanical."""
        invocations: list[tuple[Any, str]] = []

        registry = InMemoryPersistentExchangePairRegistry(
            on_release=lambda pair, reason: invocations.append((pair, reason)),
        )
        pair = _make_pair()
        registry.acquire(issue_key=42, spawn=lambda: pair)

        registry.release(42, reason="exchange-complete")

        assert len(invocations) == 1
        assert invocations[0][0] is pair
        assert invocations[0][1] == "exchange-complete"

    def test_on_release_failure_does_not_raise_through(
        self, patched_close: list[Any],
    ) -> None:
        """Release is called from finally blocks; an exception from
        the worktree-removal hook must not mask the original
        exception that brought us into the finally."""
        def _explode(pair, reason):  # noqa: ANN001, ANN202, ARG001
            raise RuntimeError("worktree removal failed")

        registry = InMemoryPersistentExchangePairRegistry(on_release=_explode)
        pair = _make_pair()
        registry.acquire(issue_key=42, spawn=lambda: pair)

        # Must not raise.
        registry.release(42, reason="exchange-complete")

    def test_on_release_carries_reason_through_shutdown_all(
        self, patched_close: list[Any],
    ) -> None:
        seen_reasons: list[str] = []

        registry = InMemoryPersistentExchangePairRegistry(
            on_release=lambda _pair, reason: seen_reasons.append(reason),
        )
        registry.acquire(issue_key=10, spawn=lambda: _make_pair())
        registry.acquire(issue_key=20, spawn=lambda: _make_pair())

        registry.shutdown_all(reason="orchestrator-shutdown")

        assert seen_reasons == ["orchestrator-shutdown", "orchestrator-shutdown"]


class TestSnapshot:
    def test_snapshot_returns_one_dict_per_cached_pair(
        self, patched_close: list[Any],
    ) -> None:
        clock = SimpleNamespace(now=200.0)
        registry = InMemoryPersistentExchangePairRegistry(clock=lambda: clock.now)
        registry.acquire(
            issue_key=42,
            spawn=lambda: _make_pair(
                issue_key=42, coder_pid=1001, reviewer_pid=1002,
                reviewer_worktree=Path("/tmp/r1"),
                created_at=100.0,
            ),
        )

        snap = registry.snapshot()
        assert len(snap) == 1
        entry = snap[0]
        assert entry["coder_pid"] == 1001
        assert entry["reviewer_pid"] == 1002
        assert entry["alive"] is True
        assert entry["reviewer_worktree"] == "/tmp/r1"
        assert entry["age_seconds"] == 100.0
