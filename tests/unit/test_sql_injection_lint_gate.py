"""Regression test for the S608 SQL-injection lint gate (#6017 F6).

The ``pyproject.toml`` ruff config enables S608 so that the codebase
fails CI if someone reintroduces f-string / ``.format`` SQL
construction under ``src/``. This test pins that configuration by:

1. Running ruff against a tiny planted offender **without** a
   ``--select`` override, so the result reflects what ``make
   lint-arch`` / ``make validate`` would actually enforce.
2. Parsing ``pyproject.toml`` and asserting ``S608`` is in
   ``tool.ruff.lint.select`` — a substring check on the file would
   pass even when the rule appears only in comments or per-file
   ignores.

If ruff ever drops S608, renames it, or stops scanning the source
tree, this test fails loudly.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_ruff(target: Path) -> subprocess.CompletedProcess:
    """Invoke ruff with the repo's configuration and no ``--select`` override.

    This is the whole point: if ``S608`` is not in
    ``tool.ruff.lint.select``, ruff will NOT report it even though
    the rule exists, and this test will notice.
    """
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--config",
            str(REPO_ROOT / "pyproject.toml"),
            "--no-cache",
            str(target),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def injected_sql_file(tmp_path: Path) -> Path:
    """Write a module that unambiguously triggers S608."""
    src = tmp_path / "offender_db.py"
    src.write_text(
        'import sqlite3\n'
        '\n'
        'def bad(conn: sqlite3.Connection, user_table: str) -> None:\n'
        '    conn.execute(f"SELECT * FROM {user_table} WHERE x = 1")\n'
    )
    return src


def test_s608_is_enabled_in_project_config(injected_sql_file: Path) -> None:
    """Running ruff with the repo config (no override) must flag the planted
    f-string SQL.

    This only holds if the project config selects ``S608``.
    """
    result = _run_ruff(injected_sql_file)

    assert result.returncode != 0, (
        "ruff did not flag the planted f-string SQL under the project config "
        "— S608 has been removed from tool.ruff.lint.select.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "S608" in result.stdout


def test_pyproject_selects_s608() -> None:
    """Parse the TOML and assert ``S608`` is in ``tool.ruff.lint.select``.

    A plain substring check on the raw file would pass if ``S608``
    only appeared in a comment or a per-file-ignore entry; this test
    insists on the real select list.
    """
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    select = data["tool"]["ruff"]["lint"]["select"]
    assert "S608" in select, (
        f"S608 is not in tool.ruff.lint.select (currently: {select!r}); "
        "the SQL-injection gate from security #6017 (F6) has been removed."
    )
