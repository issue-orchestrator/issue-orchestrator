"""CI guard: golden fixtures must stay in sync with `project_timeline()`.

If a projection helper, narrative enricher, view-registry entry, or
fan-out mapping changes, the goldens' expected blocks will drift from
the canonical output of `scripts/regen_goldens.py`. This test catches
that drift before it merges.

To resolve a failure here:

    python scripts/regen_goldens.py

then `git diff` to confirm the regenerated values are intentional, and
commit alongside the source change.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REGEN_SCRIPT = REPO_ROOT / "scripts" / "regen_goldens.py"


def test_goldens_in_sync_with_projection() -> None:
    """`scripts/regen_goldens.py --check` exits 0 when fixtures are
    in sync with current projection output.

    A non-zero exit means at least one fixture's `internal_timeline:`
    or `external_timeline:` block does not match what
    `project_timeline()` + `produce_external_records()` produce given
    that fixture's `records:`. The most common cause is an unintentional
    behavior change in projection or fan-out: refactor a helper, forget
    to regenerate, fixtures now lie about what production emits.

    The fix is to run the regen script and review the resulting diff.
    """
    result = subprocess.run(
        [sys.executable, str(REGEN_SCRIPT), "--check"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "Golden fixtures are out of sync with current projection output.\n"
        "Run `python scripts/regen_goldens.py` to refresh, then commit\n"
        "the diff alongside whatever source change caused the drift.\n"
        "\n--- script stdout ---\n"
        f"{result.stdout}"
        "\n--- script stderr ---\n"
        f"{result.stderr}"
    )
