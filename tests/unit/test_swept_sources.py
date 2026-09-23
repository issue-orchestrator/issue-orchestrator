"""The rule the repo-wide source sweeps share.

Four guards read every source file in the repository, and one test writes a real
test file into `tests/unit/` and deletes it again. `tests/swept_sources` owns the
overlap. These pin the parts the sweeps' own tests do not reach.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import tests.swept_sources as swept_sources
from tests.swept_sources import (
    PROBE_DIRECTORY,
    is_transient_probe,
    swept_source_files_in,
    transient_probe_path,
)


def test_a_probe_is_recognised_only_in_the_directory_probes_are_written_to(
    tmp_path: Path,
) -> None:
    """Same filename, two places: one is a probe, the other is source.

    A prefix match alone would hide a real module from every sweep sharing this
    rule, which is why the directory is part of the identity.
    """
    probe = transient_probe_path(tmp_path, "colour")

    assert is_transient_probe(probe, root=tmp_path)
    assert not is_transient_probe(tmp_path / "src" / probe.name, root=tmp_path)
    assert probe.parent == tmp_path / PROBE_DIRECTORY


def test_a_name_that_is_not_the_generated_shape_is_source(tmp_path: Path) -> None:
    """The prefix is not enough: the label and pid shape have to match too."""
    directory = tmp_path / PROBE_DIRECTORY

    for name in (
        "_transient_probe_.py",  # no label
        "_transient_probe_colour.py",  # no pid
        "_transient_probe_colour_12_extra.py",  # trailing segment
        "_transient_probe_colour_notapid.py",  # pid not numeric
        "not_a_transient_probe_colour_12.py",  # prefix not at the start
    ):
        assert not is_transient_probe(directory / name, root=tmp_path), name


def test_a_label_the_validator_accepts_always_builds_a_recognised_probe(
    tmp_path: Path,
) -> None:
    """The writer and the reader must agree on what a probe is called.

    They did not: an earlier version validated with `str.isalnum()`, which
    accepts "café" and Arabic-Indic digits, while recognition used an ASCII
    class that rejects the name built from them. That combination produces a
    probe no sweep would skip -- so the `FileNotFoundError` race this module
    exists to prevent would have come back, silently, for any caller who passed
    a non-ASCII label. Both halves now share one character class, and this is
    the invariant that holds them together.
    """
    for label in ("autouse", "colour", "a", "A1", "sweep2", "X" * 40):
        probe = transient_probe_path(tmp_path, label)

        assert is_transient_probe(probe, root=tmp_path), label


def test_a_probe_label_has_to_name_something(tmp_path: Path) -> None:
    """Fail fast on a label that would produce an unrecognisable name.

    The unicode cases are the ones `str.isalnum()` let through.
    """
    for label in (
        "",
        "has space",
        "has_underscore",
        "has-dash",
        "café",  # isalnum() -> True
        "٣",  # Arabic-Indic digit three; isalnum() -> True
        "²",  # superscript two; isalnum() -> True
    ):
        with pytest.raises(ValueError, match="label"):
            transient_probe_path(tmp_path, label)


def test_the_probe_name_carries_this_process(tmp_path: Path) -> None:
    """Two xdist workers running one test must not delete each other's file."""
    assert str(os.getpid()) in transient_probe_path(tmp_path, "colour").name


def test_a_missing_directory_yields_nothing_rather_than_raising(
    tmp_path: Path,
) -> None:
    """Callers sweep a fixed list of trees; not every repo has all of them."""
    assert swept_source_files_in(tmp_path / "absent", root=tmp_path) == []


def test_the_sweep_returns_sources_sorted_and_skips_caches_and_probes(
    tmp_path: Path,
) -> None:
    """One pass over what a sweep should and should not hand back."""
    tree = tmp_path / "src"
    (tree / "pkg").mkdir(parents=True)
    (tree / "__pycache__").mkdir()
    (tmp_path / PROBE_DIRECTORY).mkdir(parents=True)

    (tree / "b.py").write_text("", encoding="utf-8")
    (tree / "a.py").write_text("", encoding="utf-8")
    (tree / "pkg" / "c.py").write_text("", encoding="utf-8")
    (tree / "__pycache__" / "cached.py").write_text("", encoding="utf-8")
    (tree / "notsource.txt").write_text("", encoding="utf-8")
    transient_probe_path(tmp_path, "colour").write_text("", encoding="utf-8")

    found = swept_source_files_in(tree, root=tmp_path)

    assert found == [tree / "a.py", tree / "b.py", tree / "pkg" / "c.py"]

    # An ordinary module sits beside the probe on purpose. Asserting only that
    # the probe is absent would pass just as well if the helper skipped the
    # whole of `tests/unit`, which would silently blind every sweep using it.
    ordinary = tmp_path / PROBE_DIRECTORY / "ordinary_test_module.py"
    ordinary.write_text("", encoding="utf-8")

    assert swept_source_files_in(tmp_path / "tests", root=tmp_path) == [ordinary]


def test_other_suffixes_are_swept_when_asked_for(tmp_path: Path) -> None:
    """`test_process_table_owner` also reads shell scripts through this."""
    tree = tmp_path / "scripts"
    tree.mkdir()
    (tree / "run.sh").write_text("", encoding="utf-8")
    (tree / "run.py").write_text("", encoding="utf-8")

    assert swept_source_files_in(tree, root=tmp_path) == [tree / "run.py"]
    assert swept_source_files_in(
        tree, root=tmp_path, suffixes=(".py", ".sh")
    ) == [tree / "run.py", tree / "run.sh"]


def test_the_probe_the_colour_test_writes_is_the_one_the_sweeps_skip() -> None:
    """Writer and sweeps must mean the same directory by "the repo root".

    Recognition compares `path.parent` against `root / PROBE_DIRECTORY`, so the
    dangerous drift is not the fail-open -- it is a false NEGATIVE. If the test
    that writes the probe computed a different root from the sweeps that read it,
    a genuine probe would stop being recognised and the `FileNotFoundError` race
    would come back with no test failing. Every root here is
    `Path(__file__).resolve().parents[N]`, which normalises symlinks, so they
    agree; this fails if any of them stops agreeing.
    """
    from tests.unit.test_terminal_color_isolation import _repo_root

    module_root = Path(swept_sources.__file__).resolve().parents[1]

    assert _repo_root() == module_root, (
        "the colour test and the swept-sources module disagree about the repo "
        f"root: {_repo_root()} vs {module_root}"
    )
    assert is_transient_probe(
        transient_probe_path(_repo_root(), "autouse"), root=module_root
    )


def test_no_tracked_file_is_shaped_like_a_probe() -> None:
    """Recognition is by name, so no committed file may wear a probe's name.

    A probe is untracked by construction: it is written during a test and
    removed in `finally`. A *tracked* file matching the shape would be hidden
    from every sweep that shares this rule -- the same fail-open the broad prefix
    had, just narrowed to one directory. Closing it from this side costs one
    `git ls-files` and needs no filesystem identity check.
    """
    root = Path(swept_sources.__file__).resolve().parents[1]
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", str(PROBE_DIRECTORY)],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")

    names = [name for name in tracked if name]
    # Without this the test passes trivially whenever `git ls-files` returns
    # nothing -- a wrong cwd, a wrong pathspec, or a non-repository checkout.
    assert len(names) > 100, (
        f"`git ls-files -- {PROBE_DIRECTORY}` returned {len(names)} paths, so "
        "this guard is not looking at the test tree"
    )

    shaped = [name for name in names if is_transient_probe(root / name, root=root)]

    assert shaped == [], (
        "these tracked files wear a transient probe's name, so every sweep "
        f"sharing this rule skips them silently: {shaped}"
    )
