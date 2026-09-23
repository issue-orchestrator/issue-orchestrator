"""One name for a test file written INTO the swept tree and removed again.

Some tests can only prove what they claim by running a real test file from a
directory the shared conftest covers -- `tests/unit/test_terminal_color_isolation.py`
proves an autouse fixture is autouse by running a child suite that never
requests it. Such a file has to live under `tests/`, so it cannot be hidden
from the repo-wide source sweeps that also walk `tests/`.

That overlap is a race. A sweep globs the tree, the probe's owner removes it in
`finally`, and the sweep then reads a path that no longer exists -- a
`FileNotFoundError` on an unrelated worker, one failure out of 17k, with
nothing in the message connecting it to the test that actually caused it.

The fix is a name both sides agree on rather than a swallowed exception: a
probe is created through `transient_probe_path` and every sweep skips what
`is_transient_probe` recognises. A sweep still fails loudly on a file that
vanishes for any other reason, because that is a real problem.
"""

from __future__ import annotations

import os
from pathlib import Path

# Leading underscore keeps pytest from collecting a stale probe as a module,
# and makes the name obviously not-source to a human reading a directory.
TRANSIENT_PROBE_PREFIX = "_transient_probe_"


def transient_probe_path(directory: Path, label: str) -> Path:
    """Path for a probe `label` writes into `directory`, unique per process.

    The pid suffix keeps two xdist workers running the same test from writing
    the same file, which would make each one's `finally` delete the other's.
    """
    if not label:
        raise ValueError("a transient probe needs a label naming what it probes")
    return directory / f"{TRANSIENT_PROBE_PREFIX}{label}_{os.getpid()}.py"


def is_transient_probe(path: Path) -> bool:
    """True for a path `transient_probe_path` produced, so sweeps can skip it."""
    return path.name.startswith(TRANSIENT_PROBE_PREFIX)
