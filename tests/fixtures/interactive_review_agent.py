"""Reusable interactive review-exchange test agent.

This script mimics a CLI agent that is launched with an optional initial
prompt argument, then stays alive and accepts subsequent round prompts on
stdin. It writes the review-exchange response file expected by the
orchestrator and, for coder roles, the completion artifact.
"""

from __future__ import annotations

import json
import os
import select
import sys
import time
from pathlib import Path

from review_exchange_identity_helper import turn_identity_from_prompt_text


response_file = Path(os.environ["ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"])
completion_path_rel = os.environ["ISSUE_ORCHESTRATOR_COMPLETION_PATH"]
review_report_file = os.environ.get("ISSUE_ORCHESTRATOR_REVIEW_REPORT_FILE")
role = os.environ.get("ISSUE_ORCHESTRATOR_AGENT_LABEL", "")
initial_prompt = " ".join(sys.argv[1:]).strip()
spawn_log = os.environ.get("STUB_SPAWN_LOG")

if os.environ.get("STUB_REQUIRE_INITIAL_PROMPT") == "1" and not initial_prompt:
    print(f"[stub-{role}] missing initial argv prompt", flush=True)
    sys.exit(8)

if spawn_log:
    spawn_log_path = Path(spawn_log)
    spawn_log_path.parent.mkdir(parents=True, exist_ok=True)
    with spawn_log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "role": role,
                    "pid": os.getpid(),
                    "initial_prompt_present": bool(initial_prompt),
                    "initial_prompt_contains_wait": (
                        "Wait for the orchestrator" in initial_prompt
                    ),
                }
            )
            + "\n"
        )

# Reviewer outcomes are scripted per-round via env so a single stub script
# drives ok / changes_requested / multi-round / max-rounds scenarios. Default:
# ``ok`` every round.
raw_outcomes = os.environ.get("STUB_REVIEWER_OUTCOMES", "ok").strip()
reviewer_script = [token.strip() or "ok" for token in raw_outcomes.split(",")]
exit_after_response_roles = {
    token.strip()
    for token in os.environ.get("STUB_EXIT_AFTER_RESPONSE_ROLES", "").split(",")
    if token.strip()
}


def _should_exit_after_response() -> bool:
    return any(token in role for token in exit_after_response_roles)


def _next_reviewer_outcome_index(local_round_index: int) -> int:
    counter_file = os.environ.get("STUB_REVIEWER_OUTCOME_COUNTER_FILE")
    if not counter_file:
        return local_round_index - 1
    counter_path = Path(counter_file)
    counter_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = int(counter_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        current = 0
    counter_path.write_text(str(current + 1), encoding="utf-8")
    return current


def _reviewer_payload(outcome: str, round_index: int) -> dict[str, object]:
    if outcome == "changes_requested":
        return {
            "response_type": "changes_requested",
            "response_text": f"Needs work (stub-reviewer round {round_index})",
            "getting_closer": True,
        }
    if outcome == "ok_with_nit":
        nit_policy = os.environ.get("STUB_NIT_POLICY", "address")
        return {
            "response_type": "ok",
            "response_text": f"LGTM with nit (stub-reviewer round {round_index})",
            "getting_closer": True,
            "decision": {
                "verdict": "approved",
                "risk": "low",
                "blocking_findings": [],
                "nits": [
                    {
                        "id": "N1",
                        "title": "Use precise wording in the audit note",
                    }
                ],
                "tests_reviewed": ["stub validation"],
                "abstraction_review": {
                    "status": "no_issues",
                    "findings": [],
                },
                "nit_policy": nit_policy,
            },
        }
    return {
        "response_type": "ok",
        "response_text": f"LGTM (stub-reviewer round {round_index})",
        "getting_closer": True,
    }


def _write_review_report_if_needed(outcome: str) -> None:
    if not review_report_file or outcome != "ok_with_nit":
        return
    report_path = Path(review_report_file)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_text = (
        "# Review Report\n\n"
        "## Findings\n\nNo blocking findings.\n\n"
        "## Nits\n\n### N1. Use precise wording in the audit note\n"
    )
    report_path.write_text(report_text, encoding="utf-8")


def _coder_payload(round_index: int) -> dict[str, object]:
    worktree = Path.cwd()
    completion_full = worktree / completion_path_rel
    completion_full.parent.mkdir(parents=True, exist_ok=True)
    completion_full.write_text(
        json.dumps(
            {
                "outcome": "completed",
                "implementation": f"stub-coder round {round_index}",
                "round": round_index,
            }
        ),
        encoding="utf-8",
    )
    return {
        "response_type": "ok",
        "response_text": f"Applied (stub-coder round {round_index})",
    }


fd = sys.stdin.fileno()
print(
    f"[stub-{role}] ready initial_prompt={bool(initial_prompt)}",
    flush=True,
)
round_index = 0

# Real prompts are multi-line; reading line-by-line would advance the script
# outcome on every line of a single prompt. Instead, batch reads until stdin
# goes quiet for a brief window and treat that whole burst as one logical prompt.
quiet_window = 0.15
while True:
    ready, _, _ = select.select([fd], [], [], None)
    if not ready:
        continue
    chunk = os.read(fd, 65536)
    if not chunk:
        break
    while True:
        ready, _, _ = select.select([fd], [], [], quiet_window)
        if not ready:
            break
        more = os.read(fd, 65536)
        if not more:
            break
        chunk += more
    prompt_text = chunk.decode("utf-8", errors="replace").strip()
    if not prompt_text:
        continue
    round_index += 1
    turn_identity = turn_identity_from_prompt_text(prompt_text)
    if "reviewer" in role:
        outcome_index = _next_reviewer_outcome_index(round_index)
        outcome = (
            reviewer_script[outcome_index]
            if outcome_index < len(reviewer_script)
            else reviewer_script[-1]
        )
        payload = _reviewer_payload(outcome, round_index)
        _write_review_report_if_needed(outcome)
    else:
        payload = _coder_payload(round_index)
    payload = {**turn_identity, **payload}
    time.sleep(0.02)
    response_file.parent.mkdir(parents=True, exist_ok=True)
    response_file.write_text(json.dumps(payload), encoding="utf-8")
    print(f"[stub-{role}] wrote round {round_index}", flush=True)
    if _should_exit_after_response():
        print(f"[stub-{role}] exiting after response", flush=True)
        sys.exit(0)
