from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _write_config(root: Path, *, max_lines: int = 2) -> None:
    config = root / "tools" / "quality_guardrails.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        textwrap.dedent(
            f"""
            version: 1
            rules:
              - id: oversized
                type: file_line_budget
                max_lines: {max_lines}
                include:
                  - src/**/*.py
              - id: policy_sites
                type: control_policy_branch_sites
                new_metric_min_value: 3
                include:
                  - src/**/*.py
                  - src/**/*.js
                terms:
                  - retry
                  - status
                  - label
                  - session_state
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def _write_ruff_config(root: Path, *, ignore_noqa: bool = True) -> None:
    config = root / "tools" / "quality_guardrails.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        textwrap.dedent(
            f"""
            version: 1
            rules:
              - id: ruff_complexity
                type: ruff_findings
                include:
                  - src/**/*.py
                select:
                  - C901
                ignore_noqa: {str(ignore_noqa).lower()}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def _write_noqa_config(root: Path) -> None:
    config = root / "tools" / "quality_guardrails.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        textwrap.dedent(
            """
            version: 1
            rules:
              - id: noqa_suppressions
                type: ruff_noqa_suppressions
                include:
                  - src/**/*.py
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


def test_new_policy_site_below_floor_does_not_fail(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    (tmp_path / "src").mkdir()
    assert _run(tmp_path, "--update-baseline").returncode == 0

    target = tmp_path / "src" / "pkg" / "new_control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--fail-on-new")

    assert result.returncode == 0, result.stderr


def test_policy_terms_do_not_match_substrings(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def f(prestatus_enabled, retryable):\n"
        "    if prestatus_enabled:\n"
        "        return retryable\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert "policy_sites:src/pkg/control.py" not in baseline["metrics"]


def test_policy_terms_match_identifier_tokens_and_plurals(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def f(statusCode, labels, session_state, sessionState):\n"
        "    if statusCode:\n"
        "        return True\n"
        "    if labels:\n"
        "        return True\n"
        "    if session_state:\n"
        "        return labels\n"
        "    if sessionState:\n"
        "        return labels\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert baseline["metrics"]["policy_sites:src/pkg/control.py"]["value"] == 4


def test_ruff_findings_are_tracked_and_ratchet_numeric_scores(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ruff_config(tmp_path)
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def decide(value):\n"
        + "".join(f"    if value == {index}:\n        return {index}\n" for index in range(10))
        + "    return None\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0

    target.write_text(
        "def decide(value):\n"
        + "".join(f"    if value == {index}:\n        return {index}\n" for index in range(11))
        + "    return None\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--fail-on-new")

    assert result.returncode == 2
    assert "src/pkg/control.py [ruff_complexity] ruff_finding increased" in result.stderr
    assert "11 -> 12" in result.stderr


def test_ruff_findings_can_inventory_noqa_suppressed_debt(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ruff_config(tmp_path, ignore_noqa=True)
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def decide(value):  # noqa: C901 - existing debt\n"
        + "".join(f"    if value == {index}:\n        return {index}\n" for index in range(10))
        + "    return None\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert baseline["metrics"]["ruff_complexity:src/pkg/control.py:C901:decide"]["value"] == 11


def test_ruff_findings_respect_noqa_by_default(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ruff_config(tmp_path, ignore_noqa=False)
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def decide(value):  # noqa: C901 - existing debt\n"
        + "".join(f"    if value == {index}:\n        return {index}\n" for index in range(10))
        + "    return None\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert baseline["metrics"] == {}


def test_ruff_noqa_suppressions_are_ratchet_tracked(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_noqa_config(tmp_path)
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        'DOCS = "# noqa: ARG001 - docs only"\n'
        "def existing(arg):  # noqa: ARG001 - existing signature\n"
        "    return None\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert len(baseline["metrics"]) == 1
    assert "docs only" not in next(iter(baseline["metrics"].values()))["detail"]

    target.write_text(
        'DOCS = "# noqa: ARG001 - docs only"\n'
        "def existing(argument):  # noqa: ARG001 - existing signature\n"
        "    return None\n"
        "def added(arg):  # noqa: ARG001 - new bypass\n"
        "    return None\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--fail-on-new")

    assert result.returncode == 2
    assert "src/pkg/control.py [noqa_suppressions] new ruff_noqa_suppression" in result.stderr
    assert "new bypass" in result.stderr
    assert "existing signature" not in result.stderr


def test_decrease_does_not_fail_and_stale_baseline_is_ignored(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    target = tmp_path / "src" / "pkg" / "control.py"
    stale = tmp_path / "src" / "pkg" / "stale.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n"
        "    if status == 'done':\n"
        "        return False\n",
        encoding="utf-8",
    )
    stale.write_text(
        "def g(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0

    target.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    stale.unlink()

    result = _run(tmp_path, "--fail-on-new")

    assert result.returncode == 0, result.stderr


def test_check_stale_reports_removed_metric(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    stale = tmp_path / "src" / "pkg" / "stale.py"
    stale.parent.mkdir(parents=True)
    stale.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0
    stale.unlink()

    result = _run(tmp_path, "--check-stale")

    assert result.returncode == 3
    assert "stale baseline entries" in result.stderr
    assert "src/pkg/stale.py [policy_sites]" in result.stderr


def test_prune_removes_only_named_stale_key(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    stale = tmp_path / "src" / "pkg" / "stale.py"
    other_stale = tmp_path / "src" / "pkg" / "other_stale.py"
    stale.parent.mkdir(parents=True)
    stale.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    other_stale.write_text(
        "def g(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0
    stale.unlink()
    other_stale.unlink()

    result = _run(tmp_path, "--prune", "policy_sites:src/pkg/stale.py", "--check-stale")

    assert result.returncode == 3
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert "policy_sites:src/pkg/stale.py" not in baseline["metrics"]
    assert "policy_sites:src/pkg/other_stale.py" in baseline["metrics"]
    assert "src/pkg/other_stale.py [policy_sites]" in result.stderr


def test_prune_rejects_current_metric_key(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    current = tmp_path / "src" / "pkg" / "current.py"
    current.parent.mkdir(parents=True)
    current.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0

    result = _run(tmp_path, "--prune", "policy_sites:src/pkg/current.py")

    assert result.returncode == 1
    assert "cannot prune current metric key" in result.stderr


def test_check_stale_fails_fast_on_malformed_baseline_entry(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    stale = tmp_path / "src" / "pkg" / "stale.py"
    stale.parent.mkdir(parents=True)
    stale.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0
    stale.unlink()
    baseline_path = tmp_path / "quality" / "guardrails-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    del baseline["metrics"]["policy_sites:src/pkg/stale.py"]["detail"]
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    result = _run(tmp_path, "--check-stale")

    assert result.returncode == 1
    assert "missing non-empty string field 'detail'" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("--update-baseline", "--accept", "policy_sites:src/pkg/control.py"),
        ("--update-baseline", "--prune", "policy_sites:src/pkg/control.py"),
        (
            "--accept",
            "policy_sites:src/pkg/control.py",
            "--prune",
            "policy_sites:src/pkg/stale.py",
        ),
    ],
)
def test_update_accept_and_prune_are_mutually_exclusive(tmp_path: Path, args: tuple[str, ...]) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "src").mkdir()

    result = _run(tmp_path, *args)

    assert result.returncode == 1
    assert "--update-baseline, --accept, and --prune cannot be combined" in result.stderr


def test_missing_baseline_returns_error(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "src").mkdir()

    result = _run(tmp_path, "--fail-on-new")

    assert result.returncode == 1
    assert "baseline not found" in result.stderr


def test_json_output_includes_metrics_and_violations(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
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

    result = _run(tmp_path, "--fail-on-new", "--format", "json")

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["metrics"]
    assert payload["stale_entries"] == []
    assert payload["violations"][0]["key"] == "policy_sites:src/pkg/control.py"


def test_accept_updates_only_named_baseline_key(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    existing = tmp_path / "src" / "pkg" / "existing.py"
    accepted = tmp_path / "src" / "pkg" / "accepted.py"
    existing.parent.mkdir(parents=True)
    existing.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0
    existing.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n"
        "    if status == 'done':\n"
        "        return False\n",
        encoding="utf-8",
    )
    accepted.write_text(
        "def g(status):\n"
        "    if status == 'retry':\n"
        "        return True\n"
        "    if status == 'done':\n"
        "        return False\n"
        "    if status == 'blocked':\n"
        "        return None\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--accept", "policy_sites:src/pkg/accepted.py")

    assert result.returncode == 2
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert baseline["metrics"]["policy_sites:src/pkg/accepted.py"]["value"] == 3
    assert baseline["metrics"]["policy_sites:src/pkg/existing.py"]["value"] == 1
    assert "src/pkg/existing.py [policy_sites]" in result.stderr


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


def test_javascript_policy_sites_track_multiline_conditions(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=40)
    target = tmp_path / "src" / "ui" / "view.js"
    target.parent.mkdir(parents=True)
    target.write_text(
        "function render(status, retryCount, sessionState) {\n"
        "  if (\n"
        "    status === 'retry'\n"
        "  ) return 'retry';\n"
        "  while (\n"
        "    retryCount > 0\n"
        "  ) retryCount -= 1;\n"
        "  switch (\n"
        "    sessionState\n"
        "  ) {\n"
        "    case 'retry':\n"
        "      return status;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert baseline["metrics"]["policy_sites:src/ui/view.js"]["value"] == 4


def test_javascript_policy_sites_ignore_comments_and_string_keywords(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=40)
    target = tmp_path / "src" / "ui" / "view.js"
    target.parent.mkdir(parents=True)
    target.write_text(
        "// if (status === 'retry') return true;\n"
        "/*\n"
        "switch (sessionState) {\n"
        "  case 'retry':\n"
        "}\n"
        "*/\n"
        "const text = \"if (status)\";\n"
        "if (ready /* status */) return true;\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert "policy_sites:src/ui/view.js" not in baseline["metrics"]
