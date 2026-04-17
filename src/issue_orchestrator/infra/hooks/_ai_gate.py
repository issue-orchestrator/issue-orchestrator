"""AI gate test helpers for hook adapters."""

import json
import os
import shutil
from pathlib import Path

from ...adapters.git.git_cli import GitCLI, SubprocessCommandRunner


def _test_ai_gate_env(project_root: Path) -> dict[str, str]:
    """Build environment variables for AI gate tests.

    Strips CLAUDECODE and CLAUDE_CODE_ENTRYPOINT so nested claude -p
    calls work correctly.  These env vars are set by Claude Code itself
    and cause nested invocations to suppress output.
    """
    env = os.environ.copy()
    env["ORCHESTRATOR_HOOK_PYTHONPATH"] = str(project_root / "src")
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    return env


def _init_test_ai_gate_repo(tmppath: Path) -> Path:
    """Create a temporary git repo with a bare remote and an initial commit."""
    git = GitCLI(runner=SubprocessCommandRunner(), default_timeout_s=30)

    bare_repo = tmppath / "remote.git"
    bare_repo.mkdir()
    git.run(bare_repo, ["init", "--bare"])

    work_repo = tmppath / "work"
    git.run(tmppath, ["clone", str(bare_repo), str(work_repo)])

    git.run(work_repo, ["config", "user.email", "test@test.com"])
    git.run(work_repo, ["config", "user.name", "Test User"])

    test_file = work_repo / "test.txt"
    test_file.write_text("test content\n")
    git.run(work_repo, ["add", "test.txt"])
    git.run(work_repo, ["commit", "-m", "test commit"])

    return work_repo


_TOOL_USE_HOOK_KEYS = {"PreToolUse", "BeforeTool"}
"""Hook lifecycle events that gate tool execution - needed by the AI gate test.

Lifecycle events like ``Stop`` interfere with ``--print`` mode (causing
empty output) and are excluded. Permissions and other settings are also
stripped so the gate test exercises only the hook guardrail.
"""


def _synthesize_gate_settings(src_dir: Path, dst_dir: Path) -> None:
    """Write a minimal settings.json with only tool-use hook registrations.

    Reads settings.json from *src_dir*, keeps only the hook entries whose
    lifecycle key is in ``_TOOL_USE_HOOK_KEYS``, and writes the result into
    *dst_dir*. If no relevant hooks are found (or no source settings exist)
    the destination is left without a settings file.
    """
    src_settings = src_dir / "settings.json"
    if not src_settings.exists():
        return
    try:
        settings = json.loads(src_settings.read_text())
    except (json.JSONDecodeError, OSError):
        return
    src_hooks = settings.get("hooks", {})
    gate_hooks = {k: v for k, v in src_hooks.items() if k in _TOOL_USE_HOOK_KEYS}
    if not gate_hooks:
        return
    dst_settings = dst_dir / "settings.json"
    dst_settings.write_text(json.dumps({"hooks": gate_hooks}, indent=2) + "\n")


def _copy_hook_dir(project_root: Path, work_repo: Path, hook_dir: str) -> None:
    """Copy a hook configuration directory into the AI gate test repo.

    Copies hook scripts and directories, then synthesizes a minimal
    settings.json that contains only tool-use hook registrations (e.g.
    PreToolUse, BeforeTool). Lifecycle hooks (Stop) and permissions are
    excluded because they interfere with ``--print`` mode.
    """
    src_dir = project_root / hook_dir
    if not src_dir.exists():
        raise FileNotFoundError(f"No {hook_dir} directory found in project root")
    dst_dir = work_repo / hook_dir

    def _ignore_settings(directory: str, files: list[str]) -> list[str]:
        return [f for f in files if f == "settings.json" or f == "settings.local.json"]

    shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True, ignore=_ignore_settings)
    _synthesize_gate_settings(src_dir, dst_dir)


def _detect_blocked_from_output(output: str) -> bool:
    blocked_indicators = [
        "blocked",
        "not allowed",
        "prevented",
        "hook",
        "refused",
        "denied",
        "cannot",
        "exit code 2",
        "permission",
    ]
    output_lower = output.lower()
    return any(ind in output_lower for ind in blocked_indicators)


__all__ = [
    "_copy_hook_dir",
    "_detect_blocked_from_output",
    "_init_test_ai_gate_repo",
    "_synthesize_gate_settings",
    "_test_ai_gate_env",
]
