"""What a repo-wide source sweep reads, and the one file it must not trip over.

Several guards enumerate the whole repository and read every source file:
`test_worktree_custody` (unbound `GitWorktreeManager()`), `test_process_table_owner`
(hand-built `ps` invocations), `test_working_copy_branch_contract` (unregistered
`get_current_branch`) and `test_fixture_script_deadlines` (fixture lifetimes).
All four walk `tests` as well as `src`.

One file under `tests` is not source. `test_terminal_color_isolation` proves an
autouse fixture is autouse by writing a real child suite into `tests/unit/` --
where the shared conftest applies, which is the whole point, so it cannot live
anywhere the sweeps do not walk -- and removing it in `finally`. Under xdist a
sweep runs on another worker, globs that probe, and reads a path already gone:

    FileNotFoundError: tests/unit/_autouse_probe_80119.py

One failure in 17k, on a worker whose message names nothing connected to the
cause, and it fails the whole gate.

So enumeration is owned here rather than repeated per sweep, and a probe is
recognised narrowly: the exact generated name shape, in the single directory
probes are ever written to. A module in `src` that merely starts with the same
prefix is still swept -- otherwise this exclusion would be a hole in every guard
that uses it. Anything that vanishes for any other reason still raises, because
that is a real problem and not one to paper over.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path

# The one directory a transient probe is ever written to, relative to the repo
# root. `tests/unit` is where the shared conftest applies, which is what the
# probe's owner needs; narrowing recognition to it keeps the skip from reaching
# `src`, `scripts` or `tools`.
PROBE_DIRECTORY = Path("tests") / "unit"

# `label` is what the probe is for, and the pid keeps two xdist workers running
# the same test from deleting each other's file. Anchored, so a real module that
# merely starts with the prefix does not match.
_PROBE_NAME = re.compile(r"_transient_probe_[A-Za-z0-9]+_[0-9]+\.py")


def transient_probe_path(root: Path, label: str) -> Path:
    """Where `label`'s probe goes: the one place the sweeps tolerate one.

    Takes the repo root rather than a directory so a caller cannot put a probe
    somewhere the sweeps would still read it.
    """
    if not label or not label.isalnum():
        raise ValueError(
            f"a probe label names what it probes, in letters and digits: {label!r}"
        )
    return root / PROBE_DIRECTORY / f"_transient_probe_{label}_{os.getpid()}.py"


def is_transient_probe(path: Path, *, root: Path) -> bool:
    """True only for a probe `transient_probe_path` would produce under `root`."""
    if path.parent != root / PROBE_DIRECTORY:
        return False
    return _PROBE_NAME.fullmatch(path.name) is not None


def swept_source_files_in(
    directory: Path, *, root: Path, suffixes: Sequence[str] = (".py",)
) -> list[Path]:
    """Every source file under `directory` a sweep should read, sorted.

    Skips `__pycache__`, a missing `directory` (which yields nothing rather than
    raising, so a caller need not pre-check), and transient probes.
    """
    found: list[Path] = []
    for suffix in suffixes:
        for path in directory.rglob(f"*{suffix}"):
            if "__pycache__" in path.parts:
                continue
            if is_transient_probe(path, root=root):
                continue
            found.append(path)
    return sorted(found)
