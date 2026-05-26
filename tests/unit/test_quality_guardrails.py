from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import textwrap


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _write_config(root: Path) -> None:
    config = root / "tools" / "quality_guardrails.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        textwrap.dedent(
            """
            version: 1
            rules:
              - id: oversized
                type: file_line_budget
                max_lines: 2
                include:
                  - src/**/*.py
              - id: policy_sites
                type: control_policy_branch_sites
                include:
                  - src/**/*.py
                  - src/**/*.js
                terms:
                  - retry
                  - status
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def _copy_runner(root: Path) -> None:
    tools_dir = root / "tools"
    tools_dir.mkdir(exist_ok=True)
    source = _repo_root() / "tools" / "quality_guardrails.py"
    (tools_dir / "quality_guardrails.py").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "tools/quality_guardrails.py", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_update_baseline_records_current_metrics(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path)
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert baseline["metrics"]["oversized:src/pkg/control.py"]["value"] == 3
    assert baseline["metrics"]["policy_sites:src/pkg/control.py"]["value"] == 1


def test_fail_on_new_blocks_metric_increase(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path)
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0

    target.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n"
        "    if status == 'done':\n"
        "        return False\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--fail-on-new")

    assert result.returncode == 2
    assert "oversized:src/pkg/control.py" not in result.stderr
    assert "policy_sites" in result.stderr
    assert "1 -> 2" in result.stderr


def test_fail_on_new_blocks_new_over_budget_file(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "src").mkdir()
    assert _run(tmp_path, "--update-baseline").returncode == 0

    target = tmp_path / "src" / "pkg" / "new_control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def f():\n"
        "    return 1\n"
        "def g():\n"
        "    return 2\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--fail-on-new")

    assert result.returncode == 2
    assert "new file_line_budget" in result.stderr
    assert "src/pkg/new_control.py" in result.stderr


def test_javascript_policy_sites_are_tracked(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path)
    target = tmp_path / "src" / "ui" / "view.js"
    target.parent.mkdir(parents=True)
    target.write_text(
        "function render(status) {\n"
        "  if (status === 'retry') return 'retry';\n"
        "}\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert baseline["metrics"]["policy_sites:src/ui/view.js"]["value"] == 1
