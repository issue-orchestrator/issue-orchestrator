"""Symlink-safe containment for agent-supplied validation record paths.

Extracted verbatim from ``completion_processor`` — the single consumer — so
the containment policy (see security reviews #5987 F1, #6017 P2 and
re-review-4 P1/P2) has one cohesive owner. Only paths under
``<worktree>/.issue-orchestrator`` are acceptable as a validation-record
source; everything here exists to enforce that without TOCTOU or symlink
escapes.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat as stat_module
from pathlib import Path

logger = logging.getLogger(__name__)

# Only paths under ``<worktree>/.issue-orchestrator`` are acceptable as a
# validation-record source. Agents write to this subtree as part of normal
# operation; anything outside it (``/etc/hosts``, a sibling worktree, a
# user's SSH key) should never be handed off to the manifest/copy path.
# See security #5987 F1 review + #6017 P2 re-review.
_VALIDATION_CONTAINMENT_SUBDIR = ".issue-orchestrator"

# Hard cap on bytes we'll read off an agent-supplied validation record.
# Mirrors the per-file gate in ``completion_record_validation`` so the
# TOCTOU-safe copy path also refuses absurdly large files (#6017
# re-review-3 P1).
_VALIDATION_RECORD_MAX_BYTES = 2 * 1024 * 1024


def contain_validation_record_path(
    record_path: str, worktree: Path
) -> Path | None:
    """Resolve ``record_path`` and require it to live inside the worktree.

    Returns the resolved ``Path`` when it exists, is a regular file, and
    its fully-resolved target is under ``<worktree>/.issue-orchestrator``.
    Returns ``None`` (with a log message) otherwise — the processor must
    then skip the attach step rather than copy an out-of-tree file.

    We resolve BOTH sides (the candidate path and the worktree) because
    ``worktree`` on macOS can be under ``/private/tmp`` vs ``/tmp`` etc.,
    and ``Path.resolve`` follows symlinks so an attacker-planted link
    inside ``.issue-orchestrator`` cannot escape.
    """
    try:
        worktree_resolved = Path(worktree).resolve()
    except (OSError, RuntimeError) as exc:
        logger.warning(
            "worktree %s could not be resolved: %s", worktree, exc
        )
        return None
    try:
        candidate_raw = Path(record_path)
        # Relative paths are interpreted relative to the worktree — that
        # is the form coding-done produces when the agent records a
        # worktree-local artifact; without this, ``resolve`` would
        # anchor on the orchestrator's CWD and always fail containment.
        if not candidate_raw.is_absolute():
            candidate_raw = worktree_resolved / candidate_raw
        candidate = candidate_raw.resolve()
    except (OSError, RuntimeError) as exc:
        logger.warning(
            "validation_record_path %r could not be resolved: %s",
            record_path,
            exc,
        )
        return None
    expected_root = worktree_resolved / _VALIDATION_CONTAINMENT_SUBDIR
    try:
        candidate.relative_to(expected_root)
    except ValueError:
        logger.warning(
            "validation_record_path %s resolves outside the worktree "
            "containment root %s; refusing to attach",
            candidate,
            expected_root,
        )
        return None
    if not candidate.exists():
        logger.info(
            "validation_record_path %s does not exist; skipping attach",
            candidate,
        )
        return None
    if not candidate.is_file():
        logger.warning(
            "validation_record_path %s is not a regular file; refusing to attach",
            candidate,
        )
        return None
    return candidate


def _relative_parts_under_worktree(
    record_path: str, worktree_resolved: Path
) -> tuple[str, ...] | None:
    """Convert ``record_path`` to segments below ``worktree_resolved``.

    Handles both absolute and relative inputs. Absolute paths are
    resolved (following symlinks) and required to fall under the
    worktree's real path — this keeps common setups working where the
    worktree itself is reached through a symlinked prefix (macOS
    ``/tmp`` vs ``/private/tmp``, Linux ``/var`` vs ``/private/var``).
    Resolving the input also turns any agent-planted symlink inside
    the input into its real target; if that target escapes the
    worktree, ``relative_to`` rejects it here. The subsequent
    ``O_NOFOLLOW`` walk still guards against races between this check
    and the open. Relative paths must not contain ``..``. The first
    segment is required to be the containment subdirectory. Returns
    the validated segments, or ``None`` on rejection.
    """
    raw = Path(record_path)
    if raw.is_absolute():
        try:
            resolved_raw = raw.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            logger.warning(
                "validation_record_path %r could not be resolved: %s",
                record_path,
                exc,
            )
            return None
        try:
            rel = resolved_raw.relative_to(worktree_resolved)
        except ValueError:
            logger.warning(
                "validation_record_path %s resolves to %s, outside "
                "worktree %s; refusing to attach",
                record_path,
                resolved_raw,
                worktree_resolved,
            )
            return None
    else:
        if any(part == ".." for part in raw.parts):
            logger.warning(
                "validation_record_path %r contains '..' segment",
                record_path,
            )
            return None
        rel = raw

    parts = rel.parts
    if not parts:
        logger.warning(
            "validation_record_path %r resolved to empty segments",
            record_path,
        )
        return None
    if parts[0] != _VALIDATION_CONTAINMENT_SUBDIR:
        logger.warning(
            "validation_record_path %r first segment %r is not %s; "
            "refusing to attach",
            record_path,
            parts[0],
            _VALIDATION_CONTAINMENT_SUBDIR,
        )
        return None
    if any(segment in ("", ".", "..") for segment in parts):
        logger.warning(
            "validation_record_path %r has invalid segment", record_path
        )
        return None
    return parts


def _nofollow_walk_open(
    parts: tuple[str, ...], worktree_resolved: Path, record_path: str
) -> int | None:
    """Walk ``parts`` from ``worktree_resolved`` with ``O_NOFOLLOW``.

    Returns an open fd on the final regular file, or ``None`` on
    rejection. Caller owns the returned fd.
    """
    try:
        parent_fd = os.open(
            str(worktree_resolved),
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
    except OSError as exc:
        logger.warning(
            "Could not open worktree root %s: %s", worktree_resolved, exc
        )
        return None

    dir_fds: list[int] = [parent_fd]
    try:
        for segment in parts[:-1]:
            try:
                next_fd = os.open(
                    segment,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                logger.warning(
                    "Refusing validation_record_path %s: ancestor "
                    "segment %r failed O_NOFOLLOW open (%s). Symlink "
                    "in ancestor or race between check and open.",
                    record_path,
                    segment,
                    exc,
                )
                return None
            dir_fds.append(next_fd)
            parent_fd = next_fd

        try:
            return os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            logger.warning(
                "Refusing validation_record_path %s: final open "
                "failed (%s). File missing or final component is a "
                "symlink.",
                record_path,
                exc,
            )
            return None
    finally:
        for fd in dir_fds:
            try:
                os.close(fd)
            except OSError:
                pass


def _fd_is_safe_regular_file(fd: int, record_path: str) -> bool:
    """Reject non-regular or oversize files behind ``fd``."""
    try:
        st = os.fstat(fd)
    except OSError as exc:
        logger.warning(
            "fstat failed on validation record %s: %s", record_path, exc
        )
        return False
    if not stat_module.S_ISREG(st.st_mode):
        logger.warning(
            "validation_record_path %s is not a regular file after "
            "O_NOFOLLOW walk",
            record_path,
        )
        return False
    if st.st_size > _VALIDATION_RECORD_MAX_BYTES:
        logger.warning(
            "Validation record %s is %d bytes, exceeds cap %d",
            record_path,
            st.st_size,
            _VALIDATION_RECORD_MAX_BYTES,
        )
        return False
    return True


def open_contained_validation_record(
    record_path: str, worktree: Path
) -> int | None:
    """Open ``record_path`` by symlink-safe walk under ``worktree``.

    ``Path.resolve`` + ``relative_to`` establishes containment at a
    point in time, but reopening by pathname later (``os.open(path,
    O_NOFOLLOW)``) only refuses a symlink in the *final* component —
    an attacker who swaps an ancestor directory for a symlink between
    check and open still wins. Previously this was the bypass flagged
    in #6017 re-review-4 P1.

    The fix: never reopen by path. Walk from the worktree root,
    opening each path component with ``O_NOFOLLOW | O_CLOEXEC`` on
    directories (``O_DIRECTORY``) and on the final regular file. Any
    symlink at any level trips ``ELOOP`` and we refuse. The returned
    fd is anchored to the real inode and is safe to stream from via
    ``os.fdopen`` without ever touching the path string again.

    Returns the open fd on success (caller owns closing it) or
    ``None`` on rejection.
    """
    if not record_path:
        return None
    if "\x00" in record_path:
        logger.warning(
            "validation_record_path %r rejected: contains null byte",
            record_path,
        )
        return None
    try:
        worktree_resolved = Path(worktree).resolve()
    except (OSError, RuntimeError) as exc:
        logger.warning("worktree %s could not be resolved: %s", worktree, exc)
        return None

    parts = _relative_parts_under_worktree(record_path, worktree_resolved)
    if parts is None:
        return None

    fd = _nofollow_walk_open(parts, worktree_resolved, record_path)
    if fd is None:
        return None
    if not _fd_is_safe_regular_file(fd, record_path):
        os.close(fd)
        return None
    return fd


def copy_from_fd(src_fd: int, dst: Path) -> bool:
    """Stream ``src_fd`` into ``dst``, closing the fd on exit.

    The caller opens ``src_fd`` through a symlink-safe path walk
    (see ``open_contained_validation_record``); this helper only
    touches the fd and the destination path, never the source path
    string again. Returns ``True`` on success.
    """
    try:
        with os.fdopen(src_fd, "rb", closefd=True) as src, open(dst, "wb") as dst_file:
            shutil.copyfileobj(src, dst_file, length=65536)
    except OSError as exc:
        logger.debug(
            "Failed to copy validation record fd to %s: %s", dst, exc
        )
        return False
    return True
