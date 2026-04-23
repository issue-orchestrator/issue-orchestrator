"""Tests for agent-supplied validation_record_path containment.

Review comment P2 on #6008 (re-review from #6017): the previous commit
relaxed ``CompletionRecord.from_dict`` to accept absolute paths because
AgentGate's own record path is absolute. That shifted the containment
check to the consumer — ``_contain_validation_record_path`` in
``completion_processor`` — which this test module pins.

An attacker-controlled completion record must not be able to cause
``/etc/hosts`` (or any other file outside the worktree) to be copied
into session artifacts or recorded in the manifest.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from issue_orchestrator.control.completion_processor import (
    _contain_validation_record_path,
)


def _make_valid_record(worktree: Path) -> Path:
    """Create a realistic validation record under ``<wt>/.issue-orchestrator``."""
    root = worktree / ".issue-orchestrator" / "validation"
    root.mkdir(parents=True)
    record = root / "deadbeef.json"
    record.write_text('{"passed": true}')
    return record


def test_accepts_record_under_issue_orchestrator(tmp_path: Path) -> None:
    record = _make_valid_record(tmp_path)

    resolved = _contain_validation_record_path(str(record), tmp_path)

    assert resolved is not None
    assert resolved == record.resolve()


def test_accepts_nested_subdirectory(tmp_path: Path) -> None:
    nested = tmp_path / ".issue-orchestrator" / "sessions" / "s1" / "rec.json"
    nested.parent.mkdir(parents=True)
    nested.write_text("{}")

    resolved = _contain_validation_record_path(str(nested), tmp_path)

    assert resolved == nested.resolve()


def test_rejects_path_outside_worktree(tmp_path: Path) -> None:
    other_worktree = tmp_path / "other"
    other_worktree.mkdir()
    outside = other_worktree / ".issue-orchestrator" / "rec.json"
    outside.parent.mkdir()
    outside.write_text("{}")
    # Worktree A should not accept a file from worktree B.
    my_worktree = tmp_path / "mine"
    my_worktree.mkdir()

    assert _contain_validation_record_path(str(outside), my_worktree) is None


def test_rejects_etc_hosts(tmp_path: Path) -> None:
    """The concrete attack from the reviewer report."""
    # /etc/hosts exists on every macOS/Linux host; use it directly.
    assert _contain_validation_record_path("/etc/hosts", tmp_path) is None


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    """A symlink planted inside the worktree can't escape containment.

    resolve() follows the link to its real target, so the containment
    check sees the escaped location and refuses it.
    """
    _make_valid_record(tmp_path)
    target_outside = tmp_path / "external-secret.json"
    target_outside.write_text("secret")
    link_inside = tmp_path / ".issue-orchestrator" / "validation" / "link.json"
    try:
        os.symlink(target_outside, link_inside)
    except OSError:
        pytest.skip("symlinks not supported in this environment")

    assert _contain_validation_record_path(str(link_inside), tmp_path) is None


def test_rejects_nonexistent_path(tmp_path: Path) -> None:
    ghost = tmp_path / ".issue-orchestrator" / "does-not-exist.json"

    assert _contain_validation_record_path(str(ghost), tmp_path) is None


def test_rejects_directory(tmp_path: Path) -> None:
    dir_inside = tmp_path / ".issue-orchestrator" / "validation"
    dir_inside.mkdir(parents=True)

    assert _contain_validation_record_path(str(dir_inside), tmp_path) is None


def test_handles_resolved_worktree_symlink(tmp_path: Path) -> None:
    """macOS /tmp is a symlink to /private/tmp — containment must still work.

    We feed the containment check an absolute path under the real
    location while the worktree argument is the symlinked form.
    ``Path.resolve`` normalizes both, so the relative_to check passes.
    """
    real_worktree = tmp_path / "real"
    real_worktree.mkdir()
    link_worktree = tmp_path / "link"
    try:
        os.symlink(real_worktree, link_worktree)
    except OSError:
        pytest.skip("symlinks not supported in this environment")

    record = _make_valid_record(real_worktree)

    resolved = _contain_validation_record_path(str(record), link_worktree)

    assert resolved is not None


# ---------------------------------------------------------------------------
# TOCTOU-safe copy path — #6017 re-review-3 P1.
# ---------------------------------------------------------------------------


def test_safe_copy_copies_regular_file(tmp_path: Path) -> None:
    """Happy path: a real regular file gets copied byte-for-byte."""
    from issue_orchestrator.control.completion_processor import (
        _safe_copy_validation_record,
    )

    src = tmp_path / "rec.json"
    src.write_text('{"ok": true}')
    dst = tmp_path / "dst.json"

    assert _safe_copy_validation_record(src, dst) is True
    assert dst.read_text() == '{"ok": true}'


def test_safe_copy_refuses_symlink_source(tmp_path: Path) -> None:
    """``O_NOFOLLOW`` on the final path segment refuses a planted symlink."""
    import os as _os

    from issue_orchestrator.control.completion_processor import (
        _safe_copy_validation_record,
    )

    outside = tmp_path / "secret.json"
    outside.write_text("secret")
    link_path = tmp_path / "link.json"
    try:
        _os.symlink(outside, link_path)
    except OSError:
        pytest.skip("symlinks not supported in this environment")
    dst = tmp_path / "dst.json"

    assert _safe_copy_validation_record(link_path, dst) is False
    assert not dst.exists()


def test_safe_copy_refuses_directory_source(tmp_path: Path) -> None:
    """``fstat`` catches a non-regular file (directory in this case)."""
    from issue_orchestrator.control.completion_processor import (
        _safe_copy_validation_record,
    )

    src = tmp_path / "dir-not-file"
    src.mkdir()
    dst = tmp_path / "dst.json"

    assert _safe_copy_validation_record(src, dst) is False
    assert not dst.exists()


def test_safe_copy_refuses_oversize_source(tmp_path: Path) -> None:
    from issue_orchestrator.control.completion_processor import (
        _VALIDATION_RECORD_MAX_BYTES,
        _safe_copy_validation_record,
    )

    src = tmp_path / "rec.json"
    src.write_bytes(b"x" * (_VALIDATION_RECORD_MAX_BYTES + 1))
    dst = tmp_path / "dst.json"

    assert _safe_copy_validation_record(src, dst) is False
    assert not dst.exists()


def test_safe_copy_race_swapping_symlink_is_refused(tmp_path: Path) -> None:
    """Simulate the TOCTOU: containment check passes, then the final
    path segment is replaced with a symlink to an outside target before
    ``_safe_copy_validation_record`` opens the fd.

    The unsafe path (``shutil.copy2``) would follow the swap; our
    fd-based helper must refuse because ``O_NOFOLLOW`` fires on the
    final component.
    """
    import os as _os

    from issue_orchestrator.control.completion_processor import (
        _safe_copy_validation_record,
    )

    outside = tmp_path / "outside-secret.json"
    outside.write_text("EXFILTRATED")
    # The "original" record that passes a hypothetical containment
    # check; we then remove it and plant a symlink at the same name.
    inside_dir = tmp_path / ".issue-orchestrator" / "validation"
    inside_dir.mkdir(parents=True)
    inside_path = inside_dir / "rec.json"
    inside_path.write_text('{"ok": true}')

    # Simulate the attacker's swap between check and copy.
    inside_path.unlink()
    try:
        _os.symlink(outside, inside_path)
    except OSError:
        pytest.skip("symlinks not supported in this environment")
    dst = tmp_path / "dst.json"

    assert _safe_copy_validation_record(inside_path, dst) is False
    # The exfiltrated content must never land in the destination.
    assert not dst.exists()
