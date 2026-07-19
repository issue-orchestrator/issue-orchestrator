"""On-demand triage CLIs — aim the tech lead by hand (ADR-0031).

Two commands share this module and the same in-process-orchestrator setup:

* ``orchestrator triage <issue#>...`` — dispatch a ``failure_investigation`` at
  one or more specific issues (:func:`cmd_triage`); and
* ``orchestrator health-review`` — run one whole-board ``health_review`` on
  demand, the manual counterpart of the timer-based periodic review
  (:func:`cmd_health_review`).

Both build their own in-process orchestrator and drive the real triage-launch
path — see :mod:`..control.triage_trigger`, whose owners reuse
``launch_triage_session`` (and, for the health review, the timer path's anchor
lifecycle) so evidence-map staging + authority are identical to a reactive
launch. Extracted from ``cli.py`` (a line-budgeted hotspot) alongside the other
per-area command modules (``cli_queue_commands``, ``cli_utility_commands``).
"""

from __future__ import annotations

import argparse
import dataclasses
import time
from typing import TYPE_CHECKING

from rich.console import Console

from .cli_support import load_config

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..infra.orchestrator import Orchestrator

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
    from ..control.triage_trigger import run_targeted_investigations

    config = load_config(args)
    _configure_one_shot_triage_run(config, label="triage")
    if _repo_lock_conflict(config, command="triage"):
        return 1
    _apply_advise_only_authority(args, config)

    orchestrator = _build_orchestrator(config)
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
        _release(orchestrator)


def cmd_health_review(args: argparse.Namespace) -> int:
    """Run one whole-board triage health review on demand (walk the floor).

    The manual counterpart of the timer-based periodic review (ADR-0031 §4).
    Runs its own in-process orchestrator, forces a health review NOW — reusing
    an already-open anchor or creating one through the same lifecycle the timer
    path uses (the interval/fingerprint debounce is bypassed, but the walked
    fingerprint is still recorded so a later timer tick does not double-fire) —
    and drives it to completion with the planner paused. ``--advise-only`` dials
    every triage authority to ``propose`` so nothing auto-executes.
    """
    from ..control.triage_trigger import run_health_review

    config = load_config(args)
    _configure_one_shot_triage_run(config, label="health-review")
    if _repo_lock_conflict(config, command="health-review"):
        return 1
    _apply_advise_only_authority(args, config)

    orchestrator = _build_orchestrator(config)
    console.print(
        "[green]Running an on-demand whole-board health review"
        " (walk the floor)...[/green]"
    )
    try:
        result = run_health_review(
            orchestrator,
            now=time.monotonic,
            sleep=time.sleep,
            timeout_s=float(args.timeout),
        )
        if result.completed:
            mark = "[green]done[/green]"
            exit_code = 0
        elif result.launched:
            mark = "[yellow]running/timeout[/yellow]"
            exit_code = 0
        else:
            mark = "[red]not launched[/red]"
            exit_code = 1
        anchor = (
            f"#{result.anchor_issue_number}"
            if result.anchor_issue_number is not None
            else "(none)"
        )
        console.print(f"  health review {anchor}: {mark} — {result.detail}")
        return exit_code
    finally:
        _release(orchestrator)


def _configure_one_shot_triage_run(config: "Config", *, label: str) -> None:
    """Shared one-shot setup: main-loop logging + hard-off the background E2E.

    A one-shot triage/health run must log like the main loop does — otherwise
    its launch/worktree/apply decisions are invisible (``cmd_start`` does this
    too). It must ALSO not trigger the heavy background E2E suite: the long-lived
    ``cmd_start`` orchestrator schedules E2E on its own cadence, but a fresh
    per-invocation orchestrator re-fires the tick's E2E trigger every run — and
    the pytest worker (a full CPU core) then starves the very triage agent we
    launched (connection drops / stalls). E2E is not governed by the session
    concurrency budget, so it must be turned off here explicitly.
    """
    from ..infra.logging_config import setup_logging

    log_file = setup_logging(
        repo_root=config.repo_root,
        level="INFO",
        console_output=False,
        log_retention_days=config.log_retention_days,
    )
    console.print(f"[dim]{label} log → {log_file}[/dim]")
    config.e2e.enabled = False


def _repo_lock_conflict(config: "Config", *, command: str) -> bool:
    """True (after printing) when another orchestrator already holds the lock.

    Each on-demand command runs its own in-process orchestrator and cannot share
    the repo lock with a running one.
    """
    from ..infra.repo_lock import is_locked, read_lock

    if not is_locked(config.repo_root):
        return False
    info = read_lock(config.repo_root)
    where = f" (pid={info.pid}, port={info.http_port})" if info else ""
    console.print(
        f"[red]An orchestrator is already running{where}.[/red] Stop it"
        f" first — `{command}` runs its own in-process orchestrator and cannot"
        " share the repo lock."
    )
    return True


def _apply_advise_only_authority(args: argparse.Namespace, config: "Config") -> None:
    """Dial every triage authority dial to ``propose`` when ``--advise-only``.

    Proposals are then surfaced/gated and nothing auto-executes, except the
    non-configurable escalate-to-human floor.
    """
    from ..infra.config_models import TriageAuthorityConfig

    if not getattr(args, "advise_only", False):
        return
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


def _build_orchestrator(config: "Config") -> "Orchestrator":
    from .bootstrap import build_orchestrator

    return build_orchestrator(config=config)


def _release(orchestrator: "Orchestrator") -> None:
    """Tear down an in-process orchestrator built for a one-shot command.

    ``build_orchestrator`` wires runtime owners and background job threads that
    the normal run loop tears down on shutdown. These commands drive work
    directly and never enter that loop, so release those resources explicitly —
    otherwise the process lingers after printing results. ``close()`` is
    idempotent and safe on every exit path.
    """
    orchestrator.close()
