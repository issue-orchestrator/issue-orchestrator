from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import textwrap


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run(tmp_path: Path, code: str, rel_path: str) -> int:
    target = tmp_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(code))

    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(exist_ok=True)
    repo_tools = _repo_root() / "tools"
    (tools_dir / "check_arch_guardrails.py").write_text(
        (repo_tools / "check_arch_guardrails.py").read_text()
    )
    (tools_dir / "ast_guardrails.yml").write_text(
        (repo_tools / "ast_guardrails.yml").read_text()
    )

    proc = subprocess.run(
        [sys.executable, "tools/check_arch_guardrails.py", "src"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    return proc.returncode


def test_blocks_subprocess_import_in_control(tmp_path: Path) -> None:
    assert _run(tmp_path, "import subprocess\n", "src/issue_orchestrator/control/x.py") == 2


def test_allows_subprocess_in_execution(tmp_path: Path) -> None:
    code = "import subprocess\nsubprocess.run(['echo','hi'])\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/execution/x.py") == 0


def test_blocks_dynamic_import(tmp_path: Path) -> None:
    assert _run(tmp_path, "__import__('subprocess')\n", "src/issue_orchestrator/domain/x.py") == 2


def test_blocks_forbidden_call(tmp_path: Path) -> None:
    code = "import subprocess\nsubprocess.run(['echo','hi'])\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/ports/x.py") == 2
