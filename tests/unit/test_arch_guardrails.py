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


def test_blocks_git_subprocess_outside_allowed(tmp_path: Path) -> None:
    code = "import subprocess\nsubprocess.run(['git','status'])\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/control/x.py") == 2


def test_allows_subprocess_in_infra_supervisor(tmp_path: Path) -> None:
    """Supervisor is allowed to use subprocess for process management."""
    code = "import subprocess\nsubprocess.run(['echo','hi'])\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/infra/supervisor.py") == 0


def test_allows_subprocess_in_infra_ai_diagnose(tmp_path: Path) -> None:
    """AI diagnose is allowed to use subprocess for invoking claude."""
    code = "import subprocess\nsubprocess.run(['claude','--print'])\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/infra/ai_diagnose.py") == 0


def test_blocks_subprocess_in_infra_generic(tmp_path: Path) -> None:
    """Generic infra files should NOT have subprocess access."""
    code = "import subprocess\nsubprocess.run(['echo','hi'])\n"
    # This should be blocked because it's not in the allow list
    assert _run(tmp_path, code, "src/issue_orchestrator/infra/some_other.py") == 2


def test_blocks_subprocess_in_domain(tmp_path: Path) -> None:
    """Domain layer must never use subprocess."""
    code = "import subprocess\nsubprocess.run(['echo','hi'])\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/domain/x.py") == 2


def test_blocks_subprocess_in_ports(tmp_path: Path) -> None:
    """Ports layer must never use subprocess."""
    code = "import subprocess\nsubprocess.run(['echo','hi'])\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/ports/x.py") == 2


def test_blocks_cached_label_reads_in_control(tmp_path: Path) -> None:
    """Control layer must use fresh label reads (no get_issue_labels)."""
    code = (
        "class X:\n"
        "    def __init__(self):\n"
        "        self.issue_tracker = None\n"
        "    def f(self):\n"
        "        self.issue_tracker.get_issue_labels(1)\n"
    )
    assert _run(tmp_path, code, "src/issue_orchestrator/control/x.py") == 2
