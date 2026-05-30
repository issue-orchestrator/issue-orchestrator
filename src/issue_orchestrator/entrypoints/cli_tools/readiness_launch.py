"""Launch a readiness assessment via the user's AI agent CLI.

The setup wizard offers this as an optional first step: point whatever agent
the user has at the readiness skill and let it run the assessment
conversationally. This is provider-neutral by construction — any agent that can
read a markdown file and follow instructions works, so we just hand it the path.

This is intentionally broader than the orchestrated provider registry
(``agent_runner_providers``). That registry exists for *automated* agent runs
and its ``build_command`` carries automation flags (``--full-auto``,
``bypassPermissions``) that are wrong for a human-supervised, conversational
assessment. Readiness is a person at a terminal driving an interactive session,
so detection here is by executable name and covers agents that are not
orchestration providers (gemini, cursor).
"""

import shutil
from pathlib import Path
from typing import Any

from ...execution.interactive_launch import run_interactive
from .setup_wizard_support import Prompter

# Known interactive agent CLIs, in preference order. Each seeds an interactive
# session from a single positional prompt argument (verified for claude/codex;
# `codex --help`: "[PROMPT] Optional user prompt to start the session").
READINESS_AGENT_CLIS: tuple[str, ...] = ("claude", "codex", "gemini", "cursor")


def readiness_skill_path() -> Path:
    """Absolute path to the packaged readiness skill.

    The skill ships under ``templates/`` (inside the wheel) so it resolves
    whether the orchestrator is run from a source checkout or a pip install.
    The repo-root ``.claude/skills/readiness`` is a symlink to this file.
    """
    # readiness_launch.py -> cli_tools -> entrypoints -> issue_orchestrator
    package_root = Path(__file__).resolve().parent.parent.parent
    return package_root / "templates" / "skills" / "readiness" / "SKILL.md"


def available_readiness_clis() -> list[str]:
    """Agent CLIs found on PATH, in preference order."""
    return [exe for exe in READINESS_AGENT_CLIS if shutil.which(exe) is not None]


def build_readiness_prompt(skill_path: Path, repo_path: Path) -> str:
    """Build the seed prompt that points the agent at the skill."""
    return (
        f"Read the readiness assessment skill at {skill_path} and run it against "
        f"this repository ({repo_path}). Follow the skill's conversational flow, "
        "and ask me before any installs, probes, or remote writes."
    )


def build_readiness_command(executable: str, prompt: str) -> list[str]:
    """Build the interactive launch argv.

    Every supported CLI seeds an interactive session from a positional prompt
    (``claude "<prompt>"``, ``codex "<prompt>"``, ...). We deliberately do not
    reuse the orchestrated providers' ``build_command`` (automation flags).
    """
    return [executable, prompt]


def ensure_readiness_skill_in_repo(repo_path: Path) -> Path:
    """Copy the packaged skill into the target repo and return the in-repo path.

    Agents sandbox file reads to their workspace: gemini hard-refuses paths
    outside the repo, and codex in a read-only sandbox does the same. Pointing
    them at the orchestrator's packaged path (outside the target repo) therefore
    works only for claude. Placing the skill inside the repo being assessed makes
    the explicit-path launch work uniformly across claude, codex, gemini, and
    cursor. Idempotent; refreshed each run so it tracks the packaged version.
    """
    destination = repo_path / ".issue-orchestrator" / "readiness" / "SKILL.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(readiness_skill_path(), destination)
    return destination


def run_readiness_assessment(
    executable: str,
    repo_path: Path,
    *,
    runner=run_interactive,
    ensure_skill=ensure_readiness_skill_in_repo,
) -> int:
    """Launch the readiness assessment interactively and return the exit code.

    The skill is first copied into the target repo so every agent can read it
    from within its workspace sandbox. The session then inherits the parent's
    stdin/stdout/stderr so the user drives it directly; the wizard resumes when
    the session ends. ``runner``/``ensure_skill`` are injectable so tests
    exercise the wiring without spawning a process or touching disk.
    """
    skill_path = ensure_skill(repo_path)
    prompt = build_readiness_prompt(skill_path, repo_path)
    command = build_readiness_command(executable, prompt)
    result = runner(command, cwd=str(repo_path))
    return result.returncode


def offer_readiness_assessment(
    prompter: Prompter,
    target_path: Path,
    *,
    dry_run: bool = False,
    available_clis: Any = available_readiness_clis,
    launcher: Any = run_readiness_assessment,
) -> None:
    """Offer to run the readiness assessment before configuring (wizard UX).

    Readiness is a conversational, agent-driven review of whether a repo is a
    good fit for AI-agent orchestration. The wizard cannot run it itself (no LLM
    in the CLI), so it points whichever agent the user has at the readiness
    skill — provider-neutral by construction.

    This is a soft advisory: the rubric is early (v0), so a poor result (or a
    launch failure) never blocks setup. ``available_clis``/``launcher`` are
    injectable for testing.
    """
    if dry_run:
        return

    prompter.print("\n--- Repo Readiness (optional) ---")
    prompter.print(
        "Before configuring, you can assess whether this repo is a good fit for"
    )
    prompter.print(
        "AI-agent orchestration (PR/CI discipline, reviewer, tests, module depth)."
    )
    prompter.print("It's a conversational review your own AI agent runs for you.")
    prompter.print(
        "Note: the readiness rubric is early (v0) — treat results as guidance, "
        "not a gate."
    )

    clis = available_clis()
    if not clis:
        prompter.print(
            "\n  No agent CLI (claude/codex/gemini/cursor) found on PATH — skipping."
        )
        prompter.print(
            f"  To run it later, point your agent at: {readiness_skill_path()}"
        )
        return

    if not prompter.yes_no("Run a readiness assessment now?", default=False):
        prompter.print(
            f"  Skipped. To run it later, point your agent at: {readiness_skill_path()}"
        )
        return

    executable = clis[0] if len(clis) == 1 else prompter.choice("Which agent?", clis)
    prompter.print(f"\n  Launching {executable} with the readiness skill...")
    prompter.print("  The wizard resumes when the session ends.\n")
    try:
        launcher(executable, target_path)
    except OSError as exc:
        prompter.print(f"\n  ⚠ Could not launch {executable}: {exc}")
        prompter.print(
            f"  Run it manually by pointing your agent at: {readiness_skill_path()}"
        )
        return
    prompter.print("\n  Readiness assessment finished. Continuing setup...")
