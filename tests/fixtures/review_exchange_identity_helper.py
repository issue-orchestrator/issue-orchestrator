"""Helpers for test agents that must echo review-exchange turn identity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def turn_identity_from_prompt_text(prompt_text: str) -> dict[str, object]:
    """Extract the turn identity from a prompt notice or full prompt text."""
    for text in _candidate_prompt_texts(prompt_text):
        identity = _identity_from_json_line(text)
        if identity is not None:
            return identity
    raise ValueError("review-exchange turn identity missing from prompt")


def _candidate_prompt_texts(prompt_text: str) -> list[str]:
    texts = [prompt_text]
    prompt_path = _prompt_path_from_notice(prompt_text)
    if prompt_path is not None and prompt_path.exists():
        texts.append(prompt_path.read_text(encoding="utf-8"))
    return texts


def _prompt_path_from_notice(prompt_text: str) -> Path | None:
    prefix = "Read the full instructions from: "
    for line in prompt_text.splitlines():
        candidate = line.strip()
        if candidate.startswith(prefix):
            return Path(candidate.removeprefix(prefix))
    return None


def _identity_from_json_line(text: str) -> dict[str, object] | None:
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{") or "turn_token" not in candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if _has_turn_identity(payload):
            return {
                "turn_token": payload["turn_token"],
                "round_index": payload["round_index"],
                "attempt_index": payload["attempt_index"],
            }
    return None


def _has_turn_identity(payload: Any) -> bool:
    return isinstance(payload, dict) and {
        "turn_token",
        "round_index",
        "attempt_index",
    }.issubset(payload)
