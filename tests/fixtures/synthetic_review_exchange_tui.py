"""Synthetic raw-mode review-exchange TUI for integration tests.

This fixture approximates the provider TUI contract without live agents:
the bootstrap prompt arrives as argv, later turns arrive over stdin, and
raw-mode stdin submits on a standalone carriage return.
"""

from __future__ import annotations

import json
import os
import sys
import termios
import time
import tty
from pathlib import Path

from review_exchange_identity_helper import turn_identity_from_prompt_text


response_file = Path(os.environ["ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"])
completion_path = Path(os.environ["ISSUE_ORCHESTRATOR_COMPLETION_PATH"])
role = os.environ.get("ISSUE_ORCHESTRATOR_AGENT_LABEL", "")
initial_prompt = " ".join(sys.argv[1:]).strip()
spawn_log = os.environ.get("SYNTHETIC_TUI_SPAWN_LOG")
reviewer_script = [
    token.strip() or "ok"
    for token in os.environ.get("SYNTHETIC_TUI_REVIEWER_OUTCOMES", "ok").split(",")
]


def _emit(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def _append_spawn_log(record: dict[str, object]) -> None:
    if not spawn_log:
        return
    log_path = Path(spawn_log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _bootstrap_prompt_blocks_setup_response(prompt: str) -> bool:
    lowered = prompt.lower()
    return (
        "setup message is not a turn" in lowered
        and "do not write" in lowered
        and "review_response_file" in lowered
    )


def _maybe_write_bootstrap_response() -> bool:
    if not initial_prompt:
        return False
    if _bootstrap_prompt_blocks_setup_response(initial_prompt):
        return False
    if os.environ.get("SYNTHETIC_TUI_WRITE_BOOTSTRAP_IF_UNGUARDED") != "1":
        return False
    response_file.parent.mkdir(parents=True, exist_ok=True)
    response_file.write_text(
        json.dumps(
            {
                "response_type": "ok",
                "getting_closer": True,
                "response_text": "Ready for review prompts.",
            }
        ),
        encoding="utf-8",
    )
    return True


# Preserve any prompt bytes the framework sent while the process was starting.
tty.setraw(sys.stdin.fileno(), termios.TCSANOW)
_emit(f"[synthetic-{role}] bootstrap received={bool(initial_prompt)}")
_emit("[synthetic] raw-mode ready")
wrote_bootstrap_response = _maybe_write_bootstrap_response()
_append_spawn_log(
    {
        "role": role,
        "pid": os.getpid(),
        "initial_prompt_present": bool(initial_prompt),
        "bootstrap_not_turn_instruction": _bootstrap_prompt_blocks_setup_response(
            initial_prompt
        ),
        "wrote_bootstrap_response": wrote_bootstrap_response,
    }
)


def _reviewer_payload(round_index: int) -> dict[str, object]:
    outcome = (
        reviewer_script[round_index - 1]
        if round_index <= len(reviewer_script)
        else reviewer_script[-1]
    )
    if outcome == "changes_requested":
        return {
            "response_type": "changes_requested",
            "response_text": f"Synthetic reviewer requested changes round {round_index}",
            "getting_closer": True,
        }
    return {
        "response_type": "ok",
        "response_text": f"Synthetic reviewer approved round {round_index}",
        "getting_closer": True,
    }


def _coder_payload(round_index: int) -> dict[str, object]:
    completion_path.parent.mkdir(parents=True, exist_ok=True)
    completion_path.write_text(
        json.dumps(
            {
                "outcome": "completed",
                "implementation": f"synthetic coder round {round_index}",
                "problems": "None",
            }
        ),
        encoding="utf-8",
    )
    return {
        "response_type": "ok",
        "response_text": f"Synthetic coder applied round {round_index}",
    }


def _should_write_stale_before_turn(round_index: int) -> bool:
    target = os.environ.get("SYNTHETIC_TUI_WRITE_STALE_BEFORE_FIRST_TURN_RESPONSE")
    if round_index != 1 or not target:
        return False
    if target == "1":
        return True
    return target in role


def _write_stale_turn_response_and_wait_for_discard() -> None:
    response_file.parent.mkdir(parents=True, exist_ok=True)
    response_file.write_text(
        json.dumps(
            {
                "response_type": "ok",
                "getting_closer": True,
                "response_text": "Stale bootstrap acknowledgement.",
            }
        ),
        encoding="utf-8",
    )
    _emit(f"[synthetic-{role}] wrote stale response")
    deadline = time.monotonic() + 5
    while response_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if response_file.exists():
        raise RuntimeError("stale response was not discarded")
    _emit(f"[synthetic-{role}] stale response discarded")


def _is_framework_prompt_inbox_notice(prompt_text: str) -> bool:
    lowered = prompt_text.lower()
    return (
        lowered.startswith("review-exchange ")
        and " turn round=" in lowered
        and " attempt=" in lowered
        and "follow that file exactly" in lowered
    )


def _write_turn_response(round_index: int, prompt_text: str) -> None:
    if _should_write_stale_before_turn(round_index):
        _write_stale_turn_response_and_wait_for_discard()
    turn_identity = turn_identity_from_prompt_text(prompt_text)
    payload = (
        _reviewer_payload(round_index)
        if "reviewer" in role
        else _coder_payload(round_index)
    )
    payload = {**turn_identity, **payload}
    response_file.parent.mkdir(parents=True, exist_ok=True)
    response_file.write_text(json.dumps(payload), encoding="utf-8")
    _emit(f"[synthetic-{role}] wrote round {round_index}")


buffer = b""
round_index = 0
while True:
    char = os.read(0, 1)
    if not char:
        break

    prompt_text = buffer.decode("utf-8", errors="replace").strip()
    submit = char == b"\r"
    # Linux can translate the final Enter to LF if the framework sends before
    # setraw takes effect; accept only the known framework notice in that case.
    if char == b"\n" and _is_framework_prompt_inbox_notice(prompt_text):
        submit = True
    if not submit:
        buffer += char
        continue

    buffer = b""
    if not prompt_text:
        continue
    round_index += 1
    _write_turn_response(round_index, prompt_text)
