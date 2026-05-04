"""Tests for orphan-tempfile cleanup in review_exchange_loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.control.review_exchange_loop import sweep_atomic_write_tempfiles
from issue_orchestrator.infra.atomic_io import atomic_write_json as _atomic_write_json


def _make_review_exchange_dir(tmp_path: Path, name: str) -> Path:
    """Construct the ``sessions/<run>/review-exchange/`` layout the sweep targets."""
    run_dir = tmp_path / "sessions" / name / "review-exchange"
    run_dir.mkdir(parents=True)
    return run_dir


def test_sweep_removes_orphan_tempfile(tmp_path: Path) -> None:
    exchange_dir = _make_review_exchange_dir(tmp_path, "run-1")
    orphan = exchange_dir / ".summary.json.abc123.tmp"
    orphan.write_text("{}")

    removed = sweep_atomic_write_tempfiles(tmp_path)

    assert removed == 1
    assert not orphan.exists()


def test_sweep_leaves_real_summary_files(tmp_path: Path) -> None:
    exchange_dir = _make_review_exchange_dir(tmp_path, "run-2")
    summary = exchange_dir / "summary.json"
    summary.write_text('{"status": "ok"}')

    sweep_atomic_write_tempfiles(tmp_path)

    assert summary.exists()


def test_sweep_ignores_tempfiles_outside_review_exchange(tmp_path: Path) -> None:
    # A dotfile elsewhere with the same pattern must not be touched — the
    # sweep should only own its own per-run artifacts.
    unrelated = tmp_path / ".cache"
    unrelated.mkdir()
    bystander = unrelated / ".random.json.xyz.tmp"
    bystander.write_text("{}")

    sweep_atomic_write_tempfiles(tmp_path)

    assert bystander.exists()


def test_sweep_is_a_noop_when_root_is_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert sweep_atomic_write_tempfiles(missing) == 0


def test_atomic_write_cleans_up_its_own_tempfile_on_success(tmp_path: Path) -> None:
    """Sanity check: the normal write path leaves no orphan for the sweep."""
    exchange_dir = _make_review_exchange_dir(tmp_path, "run-3")
    target = exchange_dir / "summary.json"

    _atomic_write_json(target, {"status": "ok"})

    assert target.exists()
    tempfiles = list(exchange_dir.glob(".summary.json.*.tmp"))
    assert tempfiles == []


def test_atomic_write_cleans_up_tempfile_on_failure(tmp_path: Path, monkeypatch) -> None:
    """If os.replace raises, the partially-written tempfile is removed."""
    import os

    exchange_dir = _make_review_exchange_dir(tmp_path, "run-4")
    target = exchange_dir / "summary.json"

    def boom(src, dst):
        raise OSError("simulated cross-device rename")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError, match="simulated"):
        _atomic_write_json(target, {"status": "ok"})

    # Failure path must not leak tempfiles.
    tempfiles = list(exchange_dir.glob(".summary.json.*.tmp"))
    assert tempfiles == []
