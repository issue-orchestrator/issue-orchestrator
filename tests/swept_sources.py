"""What a repo-wide source sweep reads, and the one file it must not trip over.

Several guards enumerate source trees and read every file in them:
`test_worktree_custody` (unbound `GitWorktreeManager()`), `test_process_table_owner`
(hand-built `ps` invocations), `test_working_copy_branch_contract` (unregistered
`get_current_branch`) and `test_fixture_script_deadlines` (fixture lifetimes).
The first three walk `src` and `tests`; the fourth walks only `tests`. What they
have in common is that all four read files under `tests/unit`.

One file there is not source. `test_terminal_color_isolation` proves an autouse
fixture is autouse by writing a real child suite into `tests/unit/` -- where the
shared conftest applies, which is the whole point, so it cannot live anywhere the
sweeps do not walk -- and removing it in `finally`. Under xdist a sweep runs on
another worker, globs that probe, and reads a path already gone (the name below
is the one the probe carried when this was diagnosed, before it moved behind
`transient_probe_path`):

    FileNotFoundError: tests/unit/_autouse_probe_80119.py

One failure in 17k, on a worker whose message names nothing connected to the
cause, and it fails the whole gate.

So enumeration is owned here rather than repeated per sweep, and a probe is
recognised narrowly: the exact generated name shape, in the single directory
probes are ever written to. A module in `src` that merely starts with the same
prefix is still swept -- otherwise this exclusion would be a hole in every guard
that uses it. Anything that vanishes for any other reason still raises, because
that is a real problem and not one to paper over.

The rule is by name and directory, not by file identity. Two things keep that
from hiding real source: `PROBE_DIRECTORY` is reserved, so nothing real is
supposed to live there at all, and `test_the_probe_directory_holds_no_tracked_files`
fails if anything is committed or staged into it.

What that does NOT establish is that no real file is ever there: an ignored or
merely untracked file in the reserved directory, with a probe's exact name,
would still be skipped. The claim is that such a file is a mistake by
declaration, not that it is impossible.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path

# The one directory a transient probe is ever written to, relative to the repo
# root, and RESERVED for that: no real source belongs here, which is what lets a
# sweep skip a name in it without hiding anything. It sits under `tests/` so the
# shared `tests/conftest.py` still applies -- conftest reaches every descendant,
# which is what the probe's owner actually needs -- while keeping the skip away
# from the 670 real modules in `tests/unit` itself, let alone `src`.
PROBE_DIRECTORY = Path("tests") / "unit" / "_probes"

# One character class for both halves of the rule. `transient_probe_path`
# validates a label against `_LABEL` and `is_transient_probe` recognises the name
# built from it, so the writer and the reader cannot disagree about what a probe
# is called. They did once: `str.isalnum()` accepts "café" and Arabic-Indic
# digits, which this pattern then rejects -- a probe nothing recognised, and the
# race quietly back.
_LABEL = r"[A-Za-z0-9]+"

# `label` is what the probe is for, and the pid keeps two xdist workers running
# the same test from deleting each other's file. `[0-9]` rather than `\d`, which
# matches unicode digits `os.getpid()` will never produce.
_PROBE_LABEL = re.compile(_LABEL)
_PROBE_NAME = re.compile(rf"_transient_probe_{_LABEL}_[0-9]+\.py")


def transient_probe_path(root: Path, label: str) -> Path:
    """Where `label`'s probe goes: the one place the sweeps tolerate one.

    Takes the repo root rather than a directory so a caller cannot put a probe
    somewhere the sweeps would still read it.
    """
    if _PROBE_LABEL.fullmatch(label) is None:
        raise ValueError(
            "a probe label names what it probes, in ASCII letters and digits -- "
            "anything else builds a name `is_transient_probe` would not "
            f"recognise, putting the sweeps back in the race: {label!r}"
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
