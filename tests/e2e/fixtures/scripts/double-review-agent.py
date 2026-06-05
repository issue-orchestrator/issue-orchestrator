"""Deterministic E2E agent for a double review-exchange cycle.

The script is intentionally small but exercises the production command
contract:

* Initial coding sessions commit work and complete through ``coding-done``.
* Review-exchange coder turns commit rework and complete through
  ``coding-done`` using the owner-injected run directory.
* Reviewer turns write the current review artifact contract and force
  round 1 changes_requested, round 2 approved.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


RESPONSE_FILE = os.environ.get("ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE")
REVIEW_REPORT_FILE = os.environ.get("ISSUE_ORCHESTRATOR_REVIEW_REPORT_FILE")
AGENT_LABEL = os.environ.get("ISSUE_ORCHESTRATOR_AGENT_LABEL", "")
ISSUE_NUMBER = os.environ.get("ISSUE_ORCHESTRATOR_ISSUE_NUMBER", "unknown")


def main() -> None:
    if RESPONSE_FILE:
        if "reviewer" in AGENT_LABEL:
            _reviewer_loop(Path(RESPONSE_FILE))
        else:
            _coder_review_loop(Path(RESPONSE_FILE))
        return

    _initial_coding_session()


def _initial_coding_session() -> None:
    print(f"initial coding session for issue {ISSUE_NUMBER}", flush=True)
    _ensure_git_identity()
    _append_and_commit("initial", "E2E double review initial implementation")
    _coding_done("E2E double review initial implementation")


def _coder_review_loop(response_file: Path) -> None:
    round_index = 0
    for _prompt in _prompt_stream():
        round_index += 1
        print(f"coder review-exchange round {round_index}", flush=True)
        _ensure_git_identity()
        _append_and_commit(
            f"rework-{round_index}",
            f"E2E double review rework round {round_index}",
        )
        _coding_done(f"E2E double review rework round {round_index}")
        _write_json(
            response_file,
            {
                "response_type": "ok",
                "response_text": f"Applied E2E rework round {round_index}",
                "getting_closer": True,
            },
        )


def _reviewer_loop(response_file: Path) -> None:
    round_index = _read_counter()
    for _prompt in _prompt_stream():
        round_index += 1
        print(f"reviewer review-exchange round {round_index}", flush=True)
        _write_counter(round_index)
        if round_index == 1:
            _write_review_report(
                "# Review Report\n\n"
                "## Blocking Findings\n\n"
                "### F1. Rework required\n\n"
                "The first review round intentionally requests a focused "
                "rework pass so the E2E exercises the exchange handoff.\n\n"
                "## Final Abstraction Pass\n\n"
                "Final abstraction pass: no issues found.\n"
            )
            payload: dict[str, Any] = {
                "response_type": "changes_requested",
                "response_text": "Round 1 intentionally requires rework",
                "getting_closer": True,
                "decision": {
                    "verdict": "changes_requested",
                    "risk": "low",
                    "blocking_findings": [
                        {
                            "id": "F1",
                            "title": "Rework required",
                            "rationale": "Exercise the coder rework handoff.",
                        }
                    ],
                    "nits": [],
                    "tests_reviewed": ["E2E deterministic double review fixture"],
                    "abstraction_review": {
                        "status": "no_issues",
                        "findings": [],
                    },
                    "nit_policy": "surface",
                },
            }
        else:
            _write_review_report(
                "# Review Report\n\n"
                "## Findings\n\n"
                "No blocking findings remain after the rework turn.\n\n"
                "## Final Abstraction Pass\n\n"
                "Final abstraction pass: no issues found.\n"
            )
            payload = {
                "response_type": "ok",
                "response_text": "Round 2 approves the rework",
                "getting_closer": True,
                "decision": {
                    "verdict": "approved",
                    "risk": "low",
                    "blocking_findings": [],
                    "nits": [],
                    "tests_reviewed": ["E2E deterministic double review fixture"],
                    "abstraction_review": {
                        "status": "no_issues",
                        "findings": [],
                    },
                    "nit_policy": "surface",
                },
            }
        _write_json(response_file, payload)


def _prompt_stream():
    fd = sys.stdin.fileno()
    while True:
        ready, _, _ = select.select([fd], [], [], None)
        if not ready:
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            return
        while True:
            ready, _, _ = select.select([fd], [], [], 0.15)
            if not ready:
                break
            more = os.read(fd, 65536)
            if not more:
                break
            chunk += more
        prompt = chunk.decode("utf-8", errors="replace").strip()
        if prompt:
            yield prompt


def _append_and_commit(marker: str, message: str) -> None:
    path = Path("e2e-double-review.txt")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.time_ns()} issue={ISSUE_NUMBER} marker={marker}\n")
    _run(["git", "add", str(path)])
    _run(["git", "commit", "-m", message])


def _coding_done(implementation: str) -> None:
    _run(
        [
            sys.executable,
            "-m",
            "issue_orchestrator.entrypoints.cli_tools.coding_done",
            "completed",
            "--implementation",
            implementation,
            "--problems",
            "None",
        ]
    )


def _ensure_git_identity() -> None:
    if not _git_config_exists("user.email"):
        _run(["git", "config", "user.email", "e2e@example.invalid"])
    if not _git_config_exists("user.name"):
        _run(["git", "config", "user.name", "E2E Double Review"])


def _git_config_exists(name: str) -> bool:
    result = subprocess.run(
        ["git", "config", "--get", name],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _write_review_report(content: str) -> None:
    if not REVIEW_REPORT_FILE:
        return
    path = Path(REVIEW_REPORT_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _read_counter() -> int:
    path = _counter_path()
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return 0


def _write_counter(value: int) -> None:
    path = _counter_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value), encoding="utf-8")


def _counter_path() -> Path:
    return Path(".issue-orchestrator") / "e2e-double-reviewer-round.txt"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _run(argv: list[str]) -> None:
    subprocess.run(argv, check=True)


if __name__ == "__main__":
    main()
