#!/usr/bin/env python3
"""Select whether a GitHub Actions PR run needs aggregate validation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class StackCiDecision:
    run_full: bool
    reason: str


def decide_stack_ci_validation(
    event_name: str, payload: Mapping[str, Any]
) -> StackCiDecision:
    """Run conservatively except for a well-formed intermediate stack PR."""
    if event_name != "pull_request":
        return StackCiDecision(True, f"{event_name or 'unknown'} events validate fully")

    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, Mapping):
        return StackCiDecision(True, "pull-request metadata is missing or malformed")

    stack = pull_request.get("stack")
    if stack is None:
        return StackCiDecision(True, "standalone pull requests validate fully")
    if not isinstance(stack, Mapping):
        return StackCiDecision(True, "stack metadata is malformed")

    direct_base = pull_request.get("base")
    stack_base = stack.get("base")
    position = stack.get("position")
    size = stack.get("size")
    if (
        not isinstance(direct_base, Mapping)
        or not isinstance(stack_base, Mapping)
        or not _positive_integer(position)
        or not _positive_integer(size)
    ):
        return StackCiDecision(True, "stack position or base metadata is malformed")

    direct_base_ref = direct_base.get("ref")
    stack_base_ref = stack_base.get("ref")
    if (
        not isinstance(direct_base_ref, str)
        or not direct_base_ref
        or not isinstance(stack_base_ref, str)
        or not stack_base_ref
        or position > size
        or (position == 1 and direct_base_ref != stack_base_ref)
    ):
        return StackCiDecision(True, "stack position or base metadata is invalid")

    if direct_base_ref == stack_base_ref:
        return StackCiDecision(True, "the current lowest stack layer validates fully")
    if position == size:
        return StackCiDecision(True, "the cumulative top stack layer validates fully")
    return StackCiDecision(
        False,
        "aggregate validation is deferred until this intermediate layer becomes "
        "the lowest unmerged layer; the cumulative top layer is validated now",
    )


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _load_payload(path: str | None) -> Mapping[str, Any]:
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, Mapping) else {}


def _append_output(path: str, decision: StackCiDecision) -> None:
    with Path(path).open("a", encoding="utf-8") as output:
        output.write(f"run_full={'true' if decision.run_full else 'false'}\n")
        output.write(f"reason={decision.reason}\n")


def main() -> int:
    decision = decide_stack_ci_validation(
        os.environ.get("GITHUB_EVENT_NAME", ""),
        _load_payload(os.environ.get("GITHUB_EVENT_PATH")),
    )
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise RuntimeError("GITHUB_OUTPUT is required; refusing to defer validation")
    _append_output(output_path, decision)
    print(f"stack-ci: {decision.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
