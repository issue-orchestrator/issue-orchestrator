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

from issue_orchestrator.control.validation_record_containment import (
    contain_validation_record_path as _contain_validation_record_path,
)
from issue_orchestrator.domain.session_run import ValidationArtifactPaths


def _validation_artifacts(run_dir: Path) -> ValidationArtifactPaths:
    return ValidationArtifactPaths(
        run_dir=run_dir.resolve(),
        record_path=run_dir.resolve() / "validation-record.json",
        stdout_path=run_dir.resolve() / "validation-stdout.log",
        stderr_path=run_dir.resolve() / "validation-stderr.log",
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
# Symlink-safe open + copy — #6017 re-review-3 P1 and re-review-4 P1.
#
# The original helper used ``O_NOFOLLOW`` on a single ``os.open(path)``
# which only rejects a final-component symlink. An ancestor-directory
# swap still let the copy follow the new symlink outside the worktree.
# The walk-based helper opens each path component with ``O_NOFOLLOW``
# so any symlink at any level trips ``ELOOP``.
# ---------------------------------------------------------------------------


def _walk_open(worktree: Path, rel: str) -> int | None:
    """Shorthand for the opener under test."""
    from issue_orchestrator.control.validation_record_containment import (
        open_contained_validation_record as _open_contained_validation_record,
    )

    return _open_contained_validation_record(rel, worktree)


def test_walk_open_happy_path(tmp_path: Path) -> None:
    """Valid path with no symlinks: open succeeds and fd reads the file."""
    _make_valid_record(tmp_path)

    fd = _walk_open(tmp_path, ".issue-orchestrator/validation/deadbeef.json")

    assert fd is not None
    try:
        with os.fdopen(fd, "rb") as f:
            assert f.read() == b'{"passed": true}'
    finally:
        # Either fdopen consumed the fd or we still own it; fdopen
        # above takes ownership, so nothing to do here.
        pass


def test_walk_open_refuses_final_segment_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("secret")
    inside_dir = tmp_path / ".issue-orchestrator" / "validation"
    inside_dir.mkdir(parents=True)
    link_final = inside_dir / "rec.json"
    try:
        os.symlink(outside, link_final)
    except OSError:
        pytest.skip("symlinks not supported in this environment")

    assert _walk_open(tmp_path, ".issue-orchestrator/validation/rec.json") is None


def test_walk_open_refuses_ancestor_directory_symlink(tmp_path: Path) -> None:
    """Regression for #6017 re-review-4 P1.

    Contains the exact attack the reviewer reproduced: ``nested/`` is
    a real directory when containment runs, then gets renamed and
    replaced with a symlink to an outside tree before the copy. The
    walk must refuse because ``O_NOFOLLOW`` on the ancestor open
    trips ``ELOOP``.
    """
    rec_rel = ".issue-orchestrator/validation/nested/rec.json"

    # Real inside-the-worktree tree that would pass path containment.
    inside_nested = tmp_path / ".issue-orchestrator" / "validation" / "nested"
    inside_nested.mkdir(parents=True)
    inside_rec = inside_nested / "rec.json"
    inside_rec.write_text('{"ok": true}')

    # Exfiltration target.
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "rec.json").write_text("EXFILTRATED")

    # Attacker swaps ``nested/`` for a symlink to the outside dir.
    import shutil

    shutil.rmtree(inside_nested)
    try:
        os.symlink(outside_dir, inside_nested)
    except OSError:
        pytest.skip("symlinks not supported in this environment")

    fd = _walk_open(tmp_path, rec_rel)

    assert fd is None, (
        "Symlinked ancestor must be refused by the O_NOFOLLOW walk; "
        "otherwise the exfil content would become the validation record."
    )


def test_walk_open_refuses_absolute_path_outside_worktree(
    tmp_path: Path,
) -> None:
    assert _walk_open(tmp_path, "/etc/hosts") is None


def test_walk_open_accepts_absolute_path_through_symlinked_worktree_prefix(
    tmp_path: Path,
) -> None:
    """Regression for re-review-5 P2.

    On macOS ``/tmp`` is a symlink to ``/private/tmp``; on Linux
    ``/var`` is often a symlink to ``/private/var``. When the caller
    hands us an absolute record path using the symlink form and the
    worktree also uses the symlink form, containment must succeed.
    """
    real_worktree = tmp_path / "real"
    real_worktree.mkdir()
    link_worktree = tmp_path / "link"
    try:
        os.symlink(real_worktree, link_worktree)
    except OSError:
        pytest.skip("symlinks not supported in this environment")

    _make_valid_record(real_worktree)

    abs_under_link = (
        link_worktree / ".issue-orchestrator" / "validation" / "deadbeef.json"
    )
    fd = _walk_open(link_worktree, str(abs_under_link))

    assert fd is not None
    try:
        with os.fdopen(fd, "rb") as f:
            assert f.read() == b'{"passed": true}'
    finally:
        pass


def test_walk_open_refuses_dotdot_segment(tmp_path: Path) -> None:
    _make_valid_record(tmp_path)
    # Even if the resulting canonical path happens to fall inside the
    # worktree, any ``..`` segment in the supplied string should be
    # refused.
    assert _walk_open(
        tmp_path, ".issue-orchestrator/validation/../validation/deadbeef.json"
    ) is None


def test_walk_open_refuses_null_byte(tmp_path: Path) -> None:
    assert _walk_open(tmp_path, ".issue-orchestrator/validation/a\x00b.json") is None


def test_walk_open_refuses_first_segment_mismatch(tmp_path: Path) -> None:
    (tmp_path / "other-dir").mkdir()
    (tmp_path / "other-dir" / "rec.json").write_text("{}")

    assert _walk_open(tmp_path, "other-dir/rec.json") is None


def test_walk_open_refuses_directory_as_final(tmp_path: Path) -> None:
    (tmp_path / ".issue-orchestrator" / "validation").mkdir(parents=True)

    assert _walk_open(tmp_path, ".issue-orchestrator/validation") is None


def test_walk_open_refuses_oversize(tmp_path: Path) -> None:
    from issue_orchestrator.control.validation_record_containment import (
        _VALIDATION_RECORD_MAX_BYTES,
    )

    root = tmp_path / ".issue-orchestrator" / "validation"
    root.mkdir(parents=True)
    (root / "big.json").write_bytes(b"x" * (_VALIDATION_RECORD_MAX_BYTES + 1))

    assert _walk_open(tmp_path, ".issue-orchestrator/validation/big.json") is None


def test_copy_from_fd_streams_bytes(tmp_path: Path) -> None:
    from issue_orchestrator.control.validation_record_containment import copy_from_fd as _copy_from_fd

    src = tmp_path / "src.bin"
    src.write_bytes(b"payload")
    dst = tmp_path / "dst.bin"
    fd = os.open(str(src), os.O_RDONLY | os.O_CLOEXEC)

    assert _copy_from_fd(fd, dst) is True
    assert dst.read_bytes() == b"payload"


def test_attach_skips_manifest_when_copy_refuses(tmp_path: Path) -> None:
    """#6017 re-review-4 P2: a refused copy must leave no trace in the
    manifest. The manifest is a trust signal; publishing an agent-
    supplied path that was never copied into the run dir leaks the
    original path and implies an artifact that doesn't exist.
    """
    import json
    from unittest.mock import MagicMock

    from issue_orchestrator.control.completion_processor import CompletionProcessor

    # Plant a valid-looking agent-supplied path that FAILS the walk
    # because its first segment is outside ``.issue-orchestrator``.
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_rec = outside / "rec.json"
    outside_rec.write_text("EXFIL")

    run_dir = tmp_path / "_run"
    run_dir.mkdir()
    manifest_path = run_dir / "manifest.json"

    session_output = MagicMock()
    def _update_manifest(_run_dir: Path, updates: dict) -> None:
        existing: dict = {}
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text())
        existing.update(updates)
        manifest_path.write_text(json.dumps(existing))
    session_output.update_manifest.side_effect = _update_manifest

    processor = CompletionProcessor.__new__(CompletionProcessor)
    processor.session_output = session_output  # type: ignore[attr-defined]

    # Exercising the private helper is the point of this test.
    processor._attach_validation_artifacts(  # noqa: SLF001
        worktree=tmp_path,
        validation_artifacts=_validation_artifacts(run_dir),
        record=None,
        record_path=outside_rec,
    )

    assert not (run_dir / "validation-record.json").exists()
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        assert "validation_record_path" not in manifest, manifest
    assert not (run_dir / "validation-record.path").exists()


def test_attach_overwrites_stale_run_dir_record_with_authoritative_source(
    tmp_path: Path,
) -> None:
    """When the caller supplies an authoritative record_path, it must win
    over a pre-existing run-dir file. The previous behavior preferred the
    run-dir file unconditionally, which silently kept stale failed
    snapshots in place after a successful cache-hit publish gate — the
    manifest then disagreed with the gate decision and downstream
    consumers (review-exchange cache predicate, UI) read the wrong
    status.
    """
    import json
    from unittest.mock import MagicMock

    from issue_orchestrator.control.completion_processor import CompletionProcessor

    worktree = tmp_path
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True)
    stale_run_dir_record = run_dir / "validation-record.json"
    stale_run_dir_record.write_text(
        json.dumps({"passed": False, "head_sha": "stale", "exit_code": 1})
    )

    # Authoritative source lives under the worktree's validation store, so
    # the symlink-safe copy accepts it.
    store_dir = worktree / ".issue-orchestrator" / "validation"
    store_dir.mkdir(parents=True)
    authoritative_path = store_dir / "fresh.json"
    authoritative_path.write_text(
        json.dumps({"passed": True, "head_sha": "fresh", "exit_code": 0})
    )

    manifest_path = run_dir / "manifest.json"
    session_output = MagicMock()

    def _update_manifest(_run_dir: Path, updates: dict) -> None:
        existing: dict = {}
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text())
        existing.update(updates)
        manifest_path.write_text(json.dumps(existing))

    session_output.update_manifest.side_effect = _update_manifest

    processor = CompletionProcessor.__new__(CompletionProcessor)
    processor.session_output = session_output  # type: ignore[attr-defined]

    processor._attach_validation_artifacts(  # noqa: SLF001
        worktree=worktree,
        validation_artifacts=_validation_artifacts(run_dir),
        record=None,
        record_path=authoritative_path,
    )

    written = json.loads(stale_run_dir_record.read_text())
    assert written["passed"] is True
    assert written["head_sha"] == "fresh"

    manifest = json.loads(manifest_path.read_text())
    assert manifest["validation_record_path"] == str(stale_run_dir_record)


def test_attach_does_not_truncate_when_record_path_is_run_dir_record(
    tmp_path: Path,
) -> None:
    """When the caller supplies the run-dir record itself as
    ``record_path`` (e.g., a gate that already wrote the authoritative
    result there), ``_attach_validation_artifacts`` must not invoke the
    fd-copy: ``_copy_from_fd`` opens the destination with ``"wb"``,
    which truncates it before the source fd finishes streaming and
    leaves an empty JSON file. The helper must detect source==destination
    and just attach the existing file."""
    import json
    from unittest.mock import MagicMock

    from issue_orchestrator.control.completion_processor import CompletionProcessor

    worktree = tmp_path
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True)
    record_path = run_dir / "validation-record.json"
    payload = {"passed": True, "head_sha": "abc", "exit_code": 0}
    record_path.write_text(json.dumps(payload))

    manifest_path = run_dir / "manifest.json"
    session_output = MagicMock()

    def _update_manifest(_run_dir: Path, updates: dict) -> None:
        existing: dict = {}
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text())
        existing.update(updates)
        manifest_path.write_text(json.dumps(existing))

    session_output.update_manifest.side_effect = _update_manifest

    processor = CompletionProcessor.__new__(CompletionProcessor)
    processor.session_output = session_output  # type: ignore[attr-defined]

    processor._attach_validation_artifacts(  # noqa: SLF001
        worktree=worktree,
        validation_artifacts=_validation_artifacts(run_dir),
        record=None,
        record_path=record_path,
    )

    # File contents must be preserved (not truncated to empty JSON).
    assert json.loads(record_path.read_text()) == payload
    manifest = json.loads(manifest_path.read_text())
    assert manifest["validation_record_path"] == str(record_path)


def test_attach_refused_copy_does_not_fall_back_to_stale_run_dir_record(
    tmp_path: Path,
) -> None:
    """When an authoritative ``record_path`` is supplied but the
    symlink-safe walk refuses it (e.g., out-of-tree path), the helper
    must NOT publish a stale run-dir snapshot as if it were the
    authoritative result. Same path-leak class as #6017 re-review-4 P2
    in reverse: the caller asked to honor a specific source; honoring
    "whatever happens to be on disk instead" is wrong."""
    import json
    from unittest.mock import MagicMock

    from issue_orchestrator.control.completion_processor import CompletionProcessor

    worktree = tmp_path
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True)
    stale_run_dir_record = run_dir / "validation-record.json"
    stale_run_dir_record.write_text(
        json.dumps({"passed": False, "head_sha": "stale-local"})
    )

    # Authoritative source lives outside .issue-orchestrator — the
    # symlink-safe walk refuses it.
    outside = worktree / "outside"
    outside.mkdir()
    rejected_source = outside / "rec.json"
    rejected_source.write_text(json.dumps({"passed": True, "head_sha": "rejected"}))

    manifest_path = run_dir / "manifest.json"
    session_output = MagicMock()

    def _update_manifest(_run_dir: Path, updates: dict) -> None:
        existing: dict = {}
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text())
        existing.update(updates)
        manifest_path.write_text(json.dumps(existing))

    session_output.update_manifest.side_effect = _update_manifest

    processor = CompletionProcessor.__new__(CompletionProcessor)
    processor.session_output = session_output  # type: ignore[attr-defined]

    processor._attach_validation_artifacts(  # noqa: SLF001
        worktree=worktree,
        validation_artifacts=_validation_artifacts(run_dir),
        record=None,
        record_path=rejected_source,
    )

    # Stale local file is preserved on disk (we don't delete it), but
    # must NOT be advertised as the authoritative result.
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        assert "validation_record_path" not in manifest, manifest
    assert not (run_dir / "validation-record.path").exists()
