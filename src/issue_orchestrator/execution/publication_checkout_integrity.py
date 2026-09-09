"""Content proof for a disposable exact checkout; Git's stat cache is not proof."""

import hashlib
import os
import stat
from pathlib import Path

from ..ports.command_runner import OutputNewlines
from ..ports.git import Git


def require_exact_checkout_content(git: Git, checkout: Path, target: str) -> None:
    """Refuse hidden index flags and compare every tracked byte to its tree blob.

    Exact publication checkouts contain canonical blob bytes. A checkout filter
    that transforms those bytes is refused, rather than declaring its unverified
    output disposable. No index flags or user Git settings are changed here.
    """
    for option in ("-v", "-f"):
        indexed = git.run(checkout, ["ls-files", option, "-z"],
                          newlines=OutputNewlines.PRESERVED).stdout
        if any(entry[0] != "H" for entry in indexed.split("\0") if entry):
            raise ValueError("publication index hides tracked content; retained")
    tree = git.run(checkout, ["ls-tree", "-r", "-z", "--full-tree", target],
                   newlines=OutputNewlines.PRESERVED).stdout
    for entry in tree.split("\0"):
        if not entry:
            continue
        metadata, relative = entry.split("\t", 1)
        mode, kind, expected = metadata.split(" ")
        path = checkout / relative
        if path.parent.resolve() != path.parent or not path.is_relative_to(checkout):
            raise ValueError("publication tracked path crosses a symlink; retained")
        actual_mode, content = _tracked_content(path)
        if kind != "blob" or mode != actual_mode:
            raise ValueError("publication tracked file type or mode changed; retained")
        digest = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        if digest != expected:
            raise ValueError("publication tracked content differs from exact commit; retained")


def _tracked_content(path: Path) -> tuple[str, bytes]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return "120000", os.fsencode(os.readlink(path))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("publication tracked entry is not a regular file; retained")
        content = stream.read()
    return ("100755" if opened.st_mode & stat.S_IXUSR else "100644"), content
