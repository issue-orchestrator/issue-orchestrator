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
    assert (
        _run(tmp_path, "import subprocess\n", "src/issue_orchestrator/control/x.py")
        == 2
    )


def test_allows_subprocess_in_execution(tmp_path: Path) -> None:
    code = "import subprocess\nsubprocess.run(['echo','hi'])\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/execution/x.py") == 0


def test_blocks_dynamic_import(tmp_path: Path) -> None:
    assert (
        _run(
            tmp_path, "__import__('subprocess')\n", "src/issue_orchestrator/domain/x.py"
        )
        == 2
    )


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


def test_blocks_github_adapter_import_in_control(tmp_path: Path) -> None:
    code = "from issue_orchestrator.adapters.github import GitHubAdapter\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/control/x.py") == 2


def test_blocks_github_adapter_import_in_entrypoint_non_composition(
    tmp_path: Path,
) -> None:
    code = "from issue_orchestrator.adapters.github import GitHubAdapter\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/entrypoints/cli.py") == 2


def test_allows_github_adapter_import_in_bootstrap(tmp_path: Path) -> None:
    code = "from issue_orchestrator.adapters.github import GitHubAdapter\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/entrypoints/bootstrap.py") == 0


def test_allows_github_adapter_import_in_provider_factory(tmp_path: Path) -> None:
    code = "from issue_orchestrator.adapters.github import GitHubAdapter\n"
    assert (
        _run(
            tmp_path,
            code,
            "src/issue_orchestrator/entrypoints/repository_host_factory.py",
        )
        == 0
    )


def test_blocks_github_symbol_reference_in_control(tmp_path: Path) -> None:
    code = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from issue_orchestrator.adapters.github import GitHubAdapter\n"
        "def f(x: GitHubAdapter) -> None:\n"
        "    return None\n"
    )
    assert _run(tmp_path, code, "src/issue_orchestrator/control/x.py") == 2


def test_blocks_raw_review_exchange_summary_get_in_control(tmp_path: Path) -> None:
    code = "def f(summary):\n    return summary.get('status')\n"
    assert (
        _run(
            tmp_path,
            code,
            "src/issue_orchestrator/control/review_exchange_cache_resolution.py",
        )
        == 2
    )


def test_blocks_raw_review_exchange_summary_get_in_execution(tmp_path: Path) -> None:
    code = "def f(cached):\n    return cached.summary.get('reason')\n"
    assert (
        _run(
            tmp_path,
            code,
            "src/issue_orchestrator/execution/session_output_adapter.py",
        )
        == 2
    )


def test_blocks_raw_review_exchange_summary_get_in_review_artifacts(
    tmp_path: Path,
) -> None:
    code = "def f(summary):\n    return summary.get('artifacts')\n"
    assert (
        _run(
            tmp_path,
            code,
            "src/issue_orchestrator/domain/review_artifacts.py",
        )
        == 2
    )


def test_blocks_review_exchange_summary_dict_parameter(tmp_path: Path) -> None:
    code = (
        "def store_review_exchange_summary(summary: dict[str, object]):\n"
        "    return summary\n"
    )
    assert (
        _run(
            tmp_path,
            code,
            "src/issue_orchestrator/execution/review_exchange_session_output.py",
        )
        == 2
    )


def test_blocks_review_exchange_outcome_dict_summary_constructor(
    tmp_path: Path,
) -> None:
    code = (
        "def f(ReviewExchangeOutcome, run_assets):\n"
        "    return ReviewExchangeOutcome(\n"
        "        status='ok', rounds=1, reason='reviewer_ok',\n"
        "        run_assets=run_assets,\n"
        "        summary={'status': 'ok'},\n"
        "    )\n"
    )
    assert (
        _run(
            tmp_path,
            code,
            "src/issue_orchestrator/control/review_exchange_cache_resolution.py",
        )
        == 2
    )


def test_blocks_persistent_pair_run_rebind(tmp_path: Path) -> None:
    code = (
        "def f(pair, binding):\n"
        "    pair.run_dir = binding.run_dir\n"
        "    pair.exchange_run_id = binding.run_id\n"
    )
    assert (
        _run(
            tmp_path,
            code,
            "src/issue_orchestrator/execution/anywhere.py",
        )
        == 2
    )
