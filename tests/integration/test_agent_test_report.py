"""Exercise live coverage verdicts with tiny deterministic pytest subprocesses."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(("source", "status", "code"), [
    ("def test_ok(): pass", "passed", 0),
    ("def test_bad(): assert False", "failed", 1),
    ("import pytest\ndef test_skip(): pytest.skip('missing provider')", "unavailable", 75),
    ("def test_quota(): assert False, 'insufficient_quota'", "unavailable", 75),
    ("import pytest\ndef test_bad(): assert False\ndef test_skip(): pytest.skip('missing')", "unavailable", 75),
    ("# no tests", "unavailable", 75),
    ("raise RuntimeError('collection broke')", "unavailable", 75),
])
def test_report_never_mistakes_missing_coverage_for_a_pass(tmp_path: Path, source: str, status: str, code: int):
    test = tmp_path / "test_fixture.py"
    test.write_text(source)
    output = tmp_path / "result.json"
    root = Path(__file__).resolve().parents[2]
    environment = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONPATH": str(root), "PYTEST_ADDOPTS": ""}
    result = subprocess.run([
        sys.executable, "-m", "pytest", str(test), "--confcutdir", str(tmp_path),
        "-c", "/dev/null", "--rootdir", str(tmp_path), "-q", "-p", "scripts.agent_test_report", "--agent-test-report", str(output),
    ], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == code, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert report["status"] == status
    if status == "failed":
        assert report["failed"] == ["test_fixture.py::test_bad"]


def test_collecting_mixed_agent_modules_never_launches_a_provider(tmp_path: Path):
    root = Path(__file__).resolve().parents[2]
    audit = tmp_path / "provider-invocations"
    launcher = """
import os, pathlib, sys
def guard(event, args):
    if event == 'subprocess.Popen':
        command = args[1]
        words = command if isinstance(command, (tuple, list)) else command.split()
        if any(pathlib.Path(str(word)).name in {'claude', 'codex'} for word in words):
            pathlib.Path(os.environ['PROVIDER_AUDIT']).write_text(repr(command))
            raise RuntimeError('Provider launch during collection')
sys.addaudithook(guard)
import pytest
raise SystemExit(pytest.main(sys.argv[1:]))
"""
    modules = [
        "tests/integration/test_claude_execution.py", "tests/integration/test_codex_execution.py",
        "tests/integration/test_live_agent_chain.py", "tests/integration/test_sandbox_os_boundary.py",
        "tests/integration/test_persistent_review_exchange_integration.py",
        "tests/simulated_scenarios/test_foreign_repo_lifecycle.py",
    ]
    environment = {**os.environ, "PROVIDER_AUDIT": str(audit), "PYTEST_ADDOPTS": ""}
    result = subprocess.run([sys.executable, "-c", launcher, "--collect-only", "-q", *modules],
        cwd=root, env=environment, capture_output=True, text=True, timeout=60)
    assert not audit.exists(), audit.read_text() if audit.exists() else ""
    assert result.returncode == 0, result.stdout + result.stderr
