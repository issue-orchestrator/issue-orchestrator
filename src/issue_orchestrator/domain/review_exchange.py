"""Pure review-exchange domain helpers shared across exchange runners.

These types and functions describe the coder↔reviewer protocol without
touching infrastructure (no file I/O, no subprocess, no event sink).
They live in the domain layer so both the active in-process exchange
runner (`control/review_exchange_loop.py`) and the upcoming persistent-
session runner (`execution/persistent_session_exchange.py`) can build
on the same types and prompt/response semantics.

Public API:
    ReviewExchangeResponse, ReviewExchangeOutcome — dataclasses
    build_reviewer_prompt, build_coder_prompt — prompt construction
    parse_exchange_response — recover a response from agent output
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .review_exchange_turn import ReviewExchangeTurnPacket


@dataclass(frozen=True)
class ReviewExchangeResponse:
    """One response produced by either role during a review-exchange round."""

    response_type: str
    response_text: str
    getting_closer: bool | None = None
    raw_json: dict[str, Any] | None = None
    raw_output: str | None = None


@dataclass(frozen=True)
class ReviewExchangeOutcome:
    """Terminal outcome of a complete review-exchange run."""

    status: str  # "ok" | "stopped" | "error"
    rounds: int
    reason: str
    reviewer_response: ReviewExchangeResponse | None = None
    exchange_dir: Path | None = None
    summary: dict[str, Any] | None = None
    cache_metadata: dict[str, str] | None = None


def build_reviewer_prompt(packet: "ReviewExchangeTurnPacket") -> str:
    """Build the reviewer's prompt for one round of the exchange.

    Consumes a ``ReviewExchangeTurnPacket`` (must have
    ``role == Role.REVIEWER``); the caller is responsible for
    constructing the packet so all per-turn inputs go through one
    typed seam rather than a free keyword-arg signature.
    """
    from .review_exchange_turn import Role
    if packet.role is not Role.REVIEWER:
        raise ValueError(
            f"build_reviewer_prompt requires Role.REVIEWER packet, got {packet.role!r}"
        )
    validation_note = ""
    if packet.require_validation:
        validation_record = packet.prompt_files.validation_record
        if validation_record is None:
            raise ValueError(
                "build_reviewer_prompt requires "
                "packet.prompt_files.validation_record when validation is required"
            )
        validation_note = (
            "Validation is required. Check "
            f"{validation_record}. Only respond ok if that file exists and has "
            "passed=true. Do not rerun validation solely to create this file; "
            "if it is missing or failed, respond changes_requested asking the "
            "coder to run validation and fix any failures."
        )
    prior = ""
    if packet.last_coder_text:
        prior += f"\nCoder response:\n{packet.last_coder_text}\n"
    if packet.last_reviewer_text:
        prior += f"\nPrevious review feedback:\n{packet.last_reviewer_text}\n"
    return (
        f"You are the reviewer in a coder↔reviewer exchange for issue #{packet.issue_number}: {packet.issue_title}.\n"
        f"Round {packet.round_index}.\n"
        f"{validation_note}\n"
        "Review the current worktree changes.\n"
        "Consider:\n"
        "A) the changes for this issue\n"
        "B) relevant context in the broader codebase\n"
        "C) any applicable .claude/skills guidance\n"
        "D) docs/ if needed for intended behavior\n"
        f"{prior}\n"
        "Write exactly one line of JSON to $ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE:\n"
        '  {"response_type":"ok","getting_closer":true,"response_text":"Looks good."}\n'
        '  {"response_type":"changes_requested","getting_closer":true,"response_text":"Fix X."}\n'
        '  {"response_type":"disagree","getting_closer":false,"response_text":"Wrong approach."}\n'
    )


def build_coder_prompt(packet: "ReviewExchangeTurnPacket") -> str:
    """Build the coder's prompt for one round of the exchange.

    Consumes a ``ReviewExchangeTurnPacket`` with
    ``role == Role.CODER`` and ``reviewer_feedback`` set. Runner is
    expected to copy the most-recent reviewer response_text into the
    packet's ``reviewer_feedback`` slot.
    """
    from .review_exchange_turn import Role
    if packet.role is not Role.CODER:
        raise ValueError(
            f"build_coder_prompt requires Role.CODER packet, got {packet.role!r}"
        )
    if packet.reviewer_feedback is None:
        raise ValueError(
            "build_coder_prompt requires packet.reviewer_feedback to be set"
        )
    return (
        f"You are the coder in a review exchange for issue #{packet.issue_number}: {packet.issue_title}.\n"
        f"Round {packet.round_index}.\n"
        "Review the feedback below and update the worktree accordingly.\n"
        "\n"
        "Steps:\n"
        "1. Make the requested changes (or prepare a disagreement).\n"
        "2. Commit all changes (clean working tree required).\n"
        "3. Run `coding-done completed --implementation '...' --problems '...'`\n"
        "4. Write one line of JSON to $ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE\n"
        "\n"
        f"Session output dir: {packet.run_dir}\n"
        f"\nReviewer feedback:\n{packet.reviewer_feedback}\n"
        "\n"
        "After coding-done succeeds, write your JSON response to "
        "$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE:\n"
        '  {"response_type":"ok","response_text":"Applied fixes..."}\n'
        '  {"response_type":"disagree","response_text":"This is wrong because..."}\n'
    )


def parse_exchange_response(stdout: str) -> ReviewExchangeResponse | None:
    """Recover a structured response from raw agent output.

    Tries the response in three places, in order: a strict last-line JSON
    object; a multiline JSON string with raw newlines we know how to repair;
    embedded JSON objects in non-JSON wrapper output. Falls through to a
    JSON-line-envelope walk for agents that wrap output in a result/message
    structure (e.g. Claude's tool-call envelope).
    """
    if not stdout:
        return None
    direct = _parse_protocol_json_from_text(stdout)
    if direct is not None:
        return _review_exchange_response_from_dict(direct, stdout)

    for envelope in _iter_json_line_envelopes(stdout):
        embedded = _parse_embedded_protocol_from_envelope(envelope)
        if embedded is not None:
            return _review_exchange_response_from_dict(embedded, stdout)
    return None


def _parse_protocol_json_from_text(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    line_match = _parse_protocol_json_from_lines(stripped)
    if line_match is not None:
        return line_match
    repaired = _parse_protocol_json_with_repaired_multiline_strings(stripped)
    if repaired is not None:
        return repaired
    return _parse_protocol_json_from_embedded_objects(stripped)


def _review_exchange_response_from_dict(
    parsed: dict[str, Any],
    raw_output: str,
) -> ReviewExchangeResponse:
    return ReviewExchangeResponse(
        response_type=parsed["response_type"],
        response_text=parsed["response_text"],
        getting_closer=parsed["getting_closer"],
        raw_json=parsed["raw_json"],
        raw_output=raw_output,
    )


def _iter_json_line_envelopes(stdout: str) -> list[dict[str, Any]]:
    envelopes: list[dict[str, Any]] = []
    for line in reversed(stdout.strip().splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{") or not candidate.endswith("}"):
            continue
        try:
            envelope = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(envelope, dict):
            envelopes.append(envelope)
    return envelopes


def _parse_embedded_protocol_from_envelope(
    envelope: dict[str, Any],
) -> dict[str, Any] | None:
    result_payload = envelope.get("result")
    if isinstance(result_payload, str):
        embedded = _parse_protocol_json_from_text(result_payload)
        if embedded is not None:
            return embedded
    return _parse_embedded_protocol_from_message(envelope.get("message"))


def _parse_embedded_protocol_from_message(
    message: object,
) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for block in reversed(content):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        embedded = _parse_protocol_json_from_text(text)
        if embedded is not None:
            return embedded
    return None


def _parse_protocol_json_from_lines(stripped: str) -> dict[str, Any] | None:
    for line in reversed(stripped.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{") or not candidate.endswith("}"):
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        normalized = _normalize_protocol_response(data)
        if normalized is not None:
            return normalized
    return None


def _parse_protocol_json_from_embedded_objects(stripped: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    matches: list[dict[str, Any]] = []
    for idx, ch in enumerate(stripped):
        if ch != "{":
            continue
        try:
            obj, end = decoder.raw_decode(stripped[idx:])
        except json.JSONDecodeError:
            continue
        if end <= 0:
            continue
        normalized = _normalize_protocol_response(obj)
        if normalized is not None:
            matches.append(normalized)
    return matches[-1] if matches else None


def _parse_protocol_json_with_repaired_multiline_strings(stripped: str) -> dict[str, Any] | None:
    """Recover common malformed JSON where agents emit raw newlines inside strings.

    Review exchange prompts ask for one-line JSON, but interactive agents
    sometimes write multi-line prose directly inside ``response_text``.  The
    content is still structurally useful, so normalize raw newlines inside JSON
    strings and try one more strict parse before declaring a protocol error.
    """
    repaired = _escape_raw_newlines_inside_json_strings(stripped)
    if repaired == stripped:
        return None
    try:
        data = json.loads(repaired)
    except json.JSONDecodeError:
        return None
    return _normalize_protocol_response(data)


def _escape_raw_newlines_inside_json_strings(text: str) -> str:
    """Escape literal CR/LF characters that appear inside quoted JSON strings."""
    chars: list[str] = []
    in_string = False
    escaped = False

    for ch in text:
        if in_string:
            if escaped:
                chars.append(ch)
                escaped = False
                continue
            if ch == "\\":
                chars.append(ch)
                escaped = True
                continue
            if ch == '"':
                chars.append(ch)
                in_string = False
                continue
            if ch == "\n":
                chars.append("\\n")
                continue
            if ch == "\r":
                chars.append("\\r")
                continue
            if ch == "\t":
                chars.append("\\t")
                continue
            chars.append(ch)
            continue

        chars.append(ch)
        if ch == '"':
            in_string = True

    return "".join(chars)


def _normalize_protocol_response(obj: object) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    response_type = str(obj.get("response_type", "")).strip()
    response_text = str(obj.get("response_text", "")).strip()
    if not response_type or not response_text:
        return None
    getting_closer = obj.get("getting_closer")
    return {
        "response_type": response_type,
        "response_text": response_text,
        "getting_closer": bool(getting_closer) if getting_closer is not None else None,
        "raw_json": obj,
    }
