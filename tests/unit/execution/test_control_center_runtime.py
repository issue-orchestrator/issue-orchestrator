"""Tests for control-center runtime helpers (execution.control_center_runtime).

The headline invariant here is the **one-orchestrator-per-repo, many-repos**
guarantee: starting an orchestrator for repo B must never stop repo A's
orchestrator. ``control_start`` is the only start path that issues a
``stop_by_port(force=True)``, and every such stop is gated on whatever
``detect_orchestrator_by_port`` returns. So the eviction guard lives entirely in
``detect_orchestrator_by_port``: it must return ``None`` for any orchestrator
that does not belong to the repo being probed. If that ever regresses (e.g. the
``repo_root`` guard is dropped), starting one repo's orchestrator could evict
another's — exactly the kind of cross-repo kill we want a fast unit test to
catch, rather than discovering it from a dead orchestrator in production.

The route-layer tests in ``test_control_api_supervisor_routes.py`` mock
``detect_orchestrator_by_port``, so they do not exercise this repo-scoping — that
is what these tests cover.
"""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.execution import control_center_runtime as ccr


def test_detect_ignores_orchestrator_belonging_to_a_different_repo(monkeypatch) -> None:
    """A live orchestrator on the probed port for a DIFFERENT repo is not detected.

    This is the cross-repo eviction guard: because ``control_start`` only stops
    what ``detect`` returns, returning ``None`` here means starting repo B can
    never target repo A's orchestrator.
    """
    monkeypatch.setattr(ccr, "_load_config_port", lambda repo, cfg: 64999)
    monkeypatch.setattr(
        ccr, "_read_json", lambda url, **_: {"repo_root": "/some/OTHER/repo"}
    )

    result = ccr.detect_orchestrator_by_port(Path("/my/repo"), "test.yaml")

    assert result is None


def test_detect_returns_none_when_nothing_answers_on_port(monkeypatch) -> None:
    monkeypatch.setattr(ccr, "_load_config_port", lambda repo, cfg: 64999)
    monkeypatch.setattr(ccr, "_read_json", lambda url, **_: None)

    assert ccr.detect_orchestrator_by_port(Path("/my/repo"), "test.yaml") is None


def test_detect_returns_none_when_no_configured_port(monkeypatch) -> None:
    monkeypatch.setattr(ccr, "_load_config_port", lambda repo, cfg: None)
    # _read_json must never be reached — if it is, this blows up loudly.
    monkeypatch.setattr(
        ccr,
        "_read_json",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not probe")),
    )

    assert ccr.detect_orchestrator_by_port(Path("/my/repo"), "test.yaml") is None


def test_detect_matches_orchestrator_for_the_same_repo(monkeypatch) -> None:
    """Same-repo orchestrator IS detected (so the guard is meaningful, not vacuous)."""
    monkeypatch.setattr(ccr, "_load_config_port", lambda repo, cfg: 64999)
    monkeypatch.setattr(
        ccr, "_read_json", lambda url, **_: {"repo_root": "/my/repo"}
    )
    # Avoid the real health probe (would hit the network).
    monkeypatch.setattr(ccr, "_annotate_orchestrator_health", lambda details, base_url: None)

    result = ccr.detect_orchestrator_by_port(Path("/my/repo"), "test.yaml")

    assert result is not None
    assert result["port"] == 64999
