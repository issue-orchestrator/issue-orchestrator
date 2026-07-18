"""``orchestrator triage`` — dispatch the tech lead at specific issues on demand.

Extracted from ``cli.py`` (a line-budgeted hotspot) alongside the other
per-area command modules (``cli_queue_commands``, ``cli_utility_commands``).
The command builds its own in-process orchestrator and drives one or more
targeted failure-investigations through the REAL triage-launch path — see
:mod:`..control.triage_trigger`, whose owner reuses ``launch_triage_session``
so evidence-map staging + authority are identical to a reactive launch.
"""

from __future__ import annotations

import argparse
import dataclasses
import time

from rich.console import Console

from .cli_support import load_config

console = Console()


def cmd_triage(args: argparse.Namespace) -> int:
    """Dispatch the triage tech lead at one or more specific issues, on demand.

    Runs its own in-process orchestrator (so it cannot share the repo lock with
    a running one), launches a failure-investigation for each named issue
    through the real triage-launch path, and drives each to completion with the
    planner paused so no other board work starts. ``--advise-only`` dials every
    triage authority to ``propose`` so nothing auto-executes (only the
    non-configurable escalate floor can).
    """
    from .bootstrap import build_orchestrator
    from ..control.triage_trigger import run_targeted_investigations
    from ..infra.config_models import TriageAuthorityConfig
    from ..infra.repo_lock import is_locked, read_lock

    config = load_config(args)

    if is_locked(config.repo_root):
        info = read_lock(config.repo_root)
        where = f" (pid={info.pid}, port={info.http_port})" if info else ""
        console.print(
            f"[red]An orchestrator is already running{where}.[/red] Stop it"
            " first — `triage` runs its own in-process orchestrator and cannot"
            " share the repo lock."
        )
        return 1

    if getattr(args, "advise_only", False):
        config.triage = dataclasses.replace(
            config.triage,
            authority=TriageAuthorityConfig(
                post_comment="propose",
                create_issue="propose",
                flag_pattern="propose",
                reset_retry="propose",
                kill_hung_session="propose",
            ),
        )
        console.print(
            "[yellow]advise-only:[/yellow] all triage actions dialed to propose"
            " — proposals are surfaced/gated, nothing auto-executes (except the"
            " escalate-to-human floor)."
        )

    orchestrator = build_orchestrator(config=config)

    console.print(
        "[green]Dispatching the tech lead at:[/green] "
        + ", ".join(f"#{n}" for n in args.issues)
    )
    try:
        results = run_targeted_investigations(
            orchestrator,
            args.issues,
            now=time.monotonic,
            sleep=time.sleep,
            timeout_s=float(args.timeout),
        )

        exit_code = 0
        for result in results:
            if result.completed:
                mark = "[green]done[/green]"
            elif result.launched:
                mark = "[yellow]running/timeout[/yellow]"
            else:
                mark = "[red]not launched[/red]"
                exit_code = 1
            console.print(f"  #{result.issue_number}: {mark} — {result.detail}")
        return exit_code
    finally:
        # ``build_orchestrator`` wires runtime owners and background job threads
        # that the normal run loop tears down on shutdown. This command drives
        # investigations directly and never enters that loop, so release those
        # resources explicitly — otherwise the process lingers after printing
        # results. ``close()`` is idempotent and safe on every exit path.
        orchestrator.close()
