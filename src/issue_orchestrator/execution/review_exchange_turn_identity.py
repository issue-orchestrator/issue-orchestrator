"""Turn identity helpers for persistent review exchange prompts."""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..domain.review_exchange_turn import ReviewExchangeTurnIdentity, Role
from .persistent_round_runner import ResponseRejection, ResponseVerifier


def prepare_turn_prompt(
    prompt_text: str,
    *,
    round_index: int,
    attempt_index: int,
) -> tuple[str, ReviewExchangeTurnIdentity]:
    """Return prompt text prefixed with the current turn identity."""
    identity = ReviewExchangeTurnIdentity(
        turn_token=secrets.token_urlsafe(16),
        round_index=round_index,
        attempt_index=attempt_index,
    )
    identity_json = json.dumps(identity.to_response_fields(), sort_keys=True)
    prompt_with_identity = (
        "Review-exchange turn identity:\n"
        f"{identity_json}\n\n"
        "Every response JSON written to "
        "$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE must include those "
        "`turn_token`, `round_index`, and `attempt_index` fields exactly. "
        "The orchestrator discards responses for any other turn.\n\n"
        f"{prompt_text.rstrip()}\n"
    )
    return prompt_with_identity, identity


def build_prompt_inbox_notice(
    *,
    role: Role,
    round_index: int,
    attempt_index: int,
    identity: ReviewExchangeTurnIdentity,
    prompt_path: Path,
) -> str:
    return (
        f"Review-exchange {role.value} turn round={round_index} "
        f"attempt={attempt_index} token={identity.turn_token} is ready.\n"
        f"Read the full instructions from: {prompt_path}\n"
        "Echo `turn_token`, `round_index`, and `attempt_index` exactly in "
        "the response JSON.\n"
        "Follow that file exactly, then write one JSON response line to "
        "$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE."
    )


def build_turn_identity_verifier(
    identity: ReviewExchangeTurnIdentity,
) -> ResponseVerifier:
    def _verify(parsed: Mapping[str, Any]) -> ResponseRejection | None:
        reason = identity.mismatch_reason(parsed)
        if reason is None:
            return None
        return ResponseRejection(
            reason=reason,
            detail=identity.mismatch_detail(parsed),
        )

    return _verify
