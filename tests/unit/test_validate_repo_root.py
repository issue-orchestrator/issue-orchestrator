"""Unit tests for ``control_api._validate_repo_root``.

Related to security issue #6017 (F9 from the original tech_lead #5987).
The Control API accepts ``repo_root`` from untrusted request bodies;
this helper is the chokepoint that normalizes and rejects bad input.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.control_api import _validate_repo_root


def test_accepts_valid_directory(tmp_path: Path) -> None:
    assert _validate_repo_root(str(tmp_path)) == tmp_path.resolve()


def test_rejects_none() -> None:
    assert _validate_repo_root(None) is None


def test_rejects_empty_string() -> None:
    assert _validate_repo_root("") is None


def test_rejects_whitespace_only() -> None:
    assert _validate_repo_root("   ") is None


def test_rejects_null_byte() -> None:
    assert _validate_repo_root("/tmp/\x00evil") is None


def test_rejects_nonexistent_path(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert _validate_repo_root(str(missing)) is None


def test_rejects_file_not_directory(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("hello")
    assert _validate_repo_root(str(f)) is None


def test_normalizes_dot_dot_segments(tmp_path: Path) -> None:
    """``..`` is resolved to its canonical target rather than rejected.

    ``Path.resolve`` collapses the traversal; the caller still has to
    own whether the resolved target is acceptable. This test pins the
    behaviour so a future refactor cannot quietly start rejecting
    legitimate paths that happen to contain ``..``.
    """
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)

    # path "<tmp>/a/b/../b" resolves back to "<tmp>/a/b"
    traversal = str(nested / ".." / "b")
    assert _validate_repo_root(traversal) == nested.resolve()


def test_follows_symlink_to_valid_target(tmp_path: Path) -> None:
    real = tmp_path / "real-repo"
    real.mkdir()
    link = tmp_path / "link-to-repo"
    try:
        os.symlink(real, link)
    except OSError:
        pytest.skip("symlinks not supported in this environment")

    resolved = _validate_repo_root(str(link))
    assert resolved == real.resolve()


def test_rejects_non_string_int() -> None:
    """Regression for #6017 re-review-3: non-string inputs must not 500."""
    assert _validate_repo_root(123) is None


def test_rejects_non_string_bytes() -> None:
    assert _validate_repo_root(b"/tmp") is None


def test_rejects_non_string_dict() -> None:
    """A client that sent ``{"repo_root": "/tmp"}`` and the handler
    forgot to pull out the inner value used to crash — handle it
    cleanly.
    """
    assert _validate_repo_root({"repo_root": "/tmp"}) is None


def test_rejects_non_string_list() -> None:
    assert _validate_repo_root(["/tmp"]) is None


def test_rejects_bool() -> None:
    """``isinstance(True, int)`` is True in Python; guard anyway so a
    ``True`` value does not slip through as a path.
    """
    assert _validate_repo_root(True) is None


def test_rejects_symlink_with_dangling_target(tmp_path: Path) -> None:
    """A symlink whose target no longer exists must be rejected."""
    missing_target = tmp_path / "gone"
    link = tmp_path / "broken-link"
    try:
        os.symlink(missing_target, link)
    except OSError:
        pytest.skip("symlinks not supported in this environment")

    assert _validate_repo_root(str(link)) is None
