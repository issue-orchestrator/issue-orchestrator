"""Tests for ``atomic_write_json``."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from issue_orchestrator.infra.atomic_json import atomic_write_json


def test_writes_payload_atomically(tmp_path: Path) -> None:
    target = tmp_path / "record.json"
    atomic_write_json(target, {"passed": True, "head_sha": "abc"})

    assert json.loads(target.read_text()) == {"passed": True, "head_sha": "abc"}


def test_overwrites_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "record.json"
    target.write_text(json.dumps({"passed": False, "head_sha": "stale"}))

    atomic_write_json(target, {"passed": True, "head_sha": "fresh"})

    assert json.loads(target.read_text()) == {"passed": True, "head_sha": "fresh"}


def test_creates_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep" / "record.json"
    atomic_write_json(target, {"ok": True})

    assert target.exists()
    assert json.loads(target.read_text()) == {"ok": True}


def test_failed_write_does_not_corrupt_existing_file(tmp_path: Path) -> None:
    """If the write fails after mkstemp, the original file must be untouched
    and no partial JSON must end up at the target path."""
    target = tmp_path / "record.json"
    original = {"passed": True, "head_sha": "preserved"}
    target.write_text(json.dumps(original))

    # Force the rename step to fail. The earlier write into the tempfile
    # succeeds; we simulate a kernel-level failure of the atomic swap so
    # we can verify the original file is undisturbed.
    with patch("issue_orchestrator.infra.atomic_json.os.replace", side_effect=OSError("nope")):
        with pytest.raises(OSError):
            atomic_write_json(target, {"passed": False, "head_sha": "lost"})

    # Original file untouched.
    assert json.loads(target.read_text()) == original
    # No orphan tempfile was left behind — the explicit failure path
    # unlinks it. A stray ``.<name>.*.tmp`` here would mean the cleanup
    # fell through.
    leaked = list(tmp_path.glob(f".{target.name}.*.tmp"))
    assert leaked == [], f"orphan tempfiles left behind: {leaked}"


def test_writes_to_sibling_tempfile_not_system_temp(tmp_path: Path) -> None:
    """Atomic rename only works within a single filesystem. We rely on the
    tempfile being a sibling of the target; verify mkstemp is called with
    ``dir=`` set to the target's parent."""
    target = tmp_path / "record.json"

    captured: dict[str, str] = {}
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(**kwargs):
        captured.update(kwargs)
        return real_mkstemp(**kwargs)

    with patch("issue_orchestrator.infra.atomic_json.tempfile.mkstemp", side_effect=spy_mkstemp):
        atomic_write_json(target, {"ok": True})

    assert captured["dir"] == str(tmp_path)
