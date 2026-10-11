"""Fault-injecting agent shim for the tech-lead exam (#7304).

One script plays every scripted role; its role and its planted fault are baked
into each agent's configured command, so a case says exactly which launch
misbehaves and how:

* ``--role coder`` — an initial coding session commits a change and completes
  through ``coding-done``. Inside a review exchange (the engine sets
  ``ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE``) it only waits on its prompt
  stream and never commits, so the validated HEAD stays the published one.
* ``--role reviewer`` — a post-publish code review approves through
  ``reviewer-done``. Inside a review exchange it applies ``--exchange-fault``:
  ``exit-silently`` exits without answering, which the engine records as a
  reviewer no-completion (the porchpin 09-23 shape, minus the dialog).

``--asks QUESTION`` makes an initial coding session end by asking the
operator QUESTION (``coding-done needs_human``) without committing (Case D,
porchpin#262). ``--pr-label LABEL`` adds LABEL to the labels a completing
coder asks its PR to carry; ``needs-human`` there asks a person to decide
before the PR merges (#7678): porchpin#364's question asked beside its PR.

``--changes-once PATH`` makes a post-publish review request changes the first
time (creating PATH) and approve every later time, so the PR goes through one
rework (Case E: rework runs under a merge hold).

With ``--until-resolved``, ``--asks`` is asked only until the tech lead
resolves the block (#7658): a coder that finds the tech lead's resolution
posted on its issue works to it and publishes instead, as a real agent
reading its issue would. With
``--pr-label needs-human`` the question rides beside the published work, in
its completion's problems.

``--gives-up`` makes a coding session end WITHOUT a completion (the engine
gives up on the item: ``session_lifecycle``, porchpin#326's shape) until the
tech lead's resolution is posted on the issue, after which it codes normally.

``--capture-prompts DIR`` copies the prompt the engine launched the session
with (its run directory's ``session-prompt.txt``) into DIR, named by issue,
task and time, before doing anything else: Case I (#8141) grades what a
conflict rework was told. The run directory and the file exist at every
engine the exam runs, so the capture is engine-independent.

``--own-file`` makes a coding session write ``exam-output-<issue>.txt``
instead of the shared ``exam-output.txt``, so items' PRs never truly conflict
(Case K, #8144: integration mode must land them all, updating the ones a merge
left behind without an agent rework).

``--hold-until PATH`` makes a session wait, before doing anything, until
PATH exists. The upgrade case (Case U) uses it to keep work mid-flight across
an engine stop: the harness creates PATH only after the candidate engine has
taken over.

Completion commands come from the ENGINE's ``scripts/`` directory, which the
engine prepends to every agent's PATH, so a run against an older engine uses
that engine's own completion contract.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

RESPONSE_FILE_ENV = "ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"
EXCHANGE_FAULTS = ("none", "exit-silently")


def log(message: str) -> None:
    print(f"[exam-agent {time.strftime('%H:%M:%S')} pid={os.getpid()}] {message}", flush=True)


def run(argv: list[str]) -> None:
    log(f"$ {' '.join(argv)}")
    subprocess.run(argv, check=True)


#: The marker the engine's ``resolve_block`` posts with its decision.
RESOLUTION_MARKER = "<!-- io:resolve-block:comment:decision="
#: An approved ``propose_decision`` posted on the issue (#7593); Case J (#8691).
OPERATOR_DECISION_MARKER = re.compile(r"<!-- io:operator-decision:\d+:decision -->")


RUN_DIR_ENV = "ISSUE_ORCHESTRATOR_RUN_DIR"
SESSION_PROMPT = "session-prompt.txt"


def capture_prompt(into: Path) -> None:
    """Copy this session's launch prompt into *into* (fail loud: the case grades it)."""
    run_dir = Path(os.environ[RUN_DIR_ENV])
    identity = json.loads((run_dir / "session-identity.json").read_text(encoding="utf-8"))
    task = str(identity.get("task", "unknown"))
    into.mkdir(parents=True, exist_ok=True)
    target = into / f"{issue_number()}-{task}-{time.time_ns()}.txt"
    target.write_text((run_dir / SESSION_PROMPT).read_text(encoding="utf-8"), encoding="utf-8")
    log(f"captured the {task} prompt into {target}")


def ask_the_operator(question: str) -> None:
    run(["coding-done", "needs_human", "--question", question])


def issue_number() -> int:
    raw = os.environ.get("ISSUE_ORCHESTRATOR_ISSUE_NUMBER") or os.environ.get("ORCHESTRATOR_ISSUE_NUMBER")
    if not raw:
        raise SystemExit("exam coder: the engine set no issue number")
    return int(raw)


def resolved_by_the_tech_lead() -> bool:
    """Whether the tech lead's resolution, or a decision the operator approved,
    is posted on this issue (fail loud)."""
    out = subprocess.run(
        ["gh", "issue", "view", str(issue_number()), "--json", "comments", "--jq", ".comments[].body"],
        check=True, capture_output=True, text=True,
    ).stdout
    resolved = RESOLUTION_MARKER in out or OPERATOR_DECISION_MARKER.search(out) is not None
    log(f"tech lead's resolution or approved decision on the issue: {resolved}")
    return resolved


def initial_coding_session(extra_pr_labels: list[str], problems: str = "None", *, own_file: bool = False) -> None:
    marker = Path(f"exam-output-{issue_number()}.txt" if own_file else "exam-output.txt")
    marker.write_text(f"tech-lead exam work item, written {time.ctime()}\n", encoding="utf-8")
    run(["git", "add", str(marker)])
    run(
        [
            "git",
            "commit",
            "--signoff",
            "-m",
            "Exam: planted work item",
            "--trailer",
            "Agent-Status: completed",
            "--trailer",
            "Agent-Implementation: exam work item",
            "--trailer",
            "Agent-Problems: None",
        ]
    )
    argv = [
        "coding-done",
        "completed",
        "--implementation",
        "Exam work item committed",
        "--problems",
        problems,
    ]
    labels = [label for label in os.environ.get("E2E_PR_LABELS", "").split(",") if label]
    labels += extra_pr_labels
    if labels:
        argv += ["--pr-labels", *labels]
    run(argv)


def idle_on_prompts() -> None:
    """Hold the exchange coder open without ever changing the worktree."""
    log("exchange coder: waiting on prompts, never committing")
    for line in sys.stdin:
        log(f"exchange coder ignored prompt line ({len(line)} chars)")


def approve_review() -> None:
    run(
        [
            "reviewer-done",
            "approved",
            "--summary",
            "Exam reviewer: approved",
            "--risk",
            "low",
        ]
    )


def request_changes_once(marker: Path) -> bool:
    """Request changes the first time only; True when it did."""
    if marker.exists():
        return False
    marker.write_text("changes requested once\n", encoding="utf-8")
    run(
        [
            "reviewer-done",
            "changes_requested",
            "--issues",
            "Exam reviewer: one round of changes before approval",
            "--risk",
            "low",
        ]
    )
    return True


def hold_until(release: Path) -> None:
    """Stay mid-flight until the harness releases the work."""
    log(f"holding until {release} exists")
    while not release.exists():
        time.sleep(2)
    log("released")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("coder", "reviewer"), required=True)
    parser.add_argument("--exchange-fault", choices=EXCHANGE_FAULTS, default="none")
    parser.add_argument("--hold-until", type=Path, default=None)
    parser.add_argument("--asks", default=None)
    parser.add_argument("--pr-label", action="append", default=[])
    parser.add_argument("--changes-once", type=Path, default=None)
    parser.add_argument("--gives-up", action="store_true")
    parser.add_argument("--until-resolved", action="store_true")
    parser.add_argument("--capture-prompts", type=Path, default=None)
    parser.add_argument("--own-file", action="store_true")
    args = parser.parse_args()
    in_exchange = bool(os.environ.get(RESPONSE_FILE_ENV))
    log(f"role={args.role} in_exchange={in_exchange} fault={args.exchange_fault}")
    if args.capture_prompts is not None and not in_exchange:
        capture_prompt(args.capture_prompts)
    if args.hold_until is not None:
        hold_until(args.hold_until)

    if args.role == "coder":
        if in_exchange:
            idle_on_prompts()
        elif args.gives_up and not resolved_by_the_tech_lead():
            log("planted fault: ending the session without a completion")
        elif args.asks and args.pr_label:
            initial_coding_session(args.pr_label, problems=f"Question for the maintainer: {args.asks}")
        elif args.asks and not (args.until_resolved and resolved_by_the_tech_lead()):
            ask_the_operator(args.asks)
        else:
            initial_coding_session(args.pr_label, own_file=args.own_file)
        return 0

    if not in_exchange:
        if args.changes_once is None or not request_changes_once(args.changes_once):
            approve_review()
        return 0
    if args.exchange_fault == "exit-silently":
        log("exchange reviewer: planted fault, exiting without a verdict")
        return 0
    raise SystemExit(
        "exam reviewer launched inside a review exchange without a planted fault;"
        " this shim only answers post-publish reviews"
    )


if __name__ == "__main__":
    sys.exit(main())
