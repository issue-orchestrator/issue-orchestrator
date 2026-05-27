"""Shared request parsing for issue-number web actions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


@dataclass(frozen=True)
class IssueNumbersPayload:
    """Parsed issue-number request payload."""

    issue_numbers: list[int] = field(default_factory=list)
    body: dict[str, Any] = field(default_factory=dict)
    error_response: JSONResponse | None = None


async def parse_issue_numbers_payload(request: Request) -> IssueNumbersPayload:
    """Parse and normalize a JSON payload containing an ``issues`` list."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return IssueNumbersPayload(
            error_response=JSONResponse({"error": "Invalid JSON"}, status_code=400)
        )

    if not isinstance(body, dict):
        return IssueNumbersPayload(
            error_response=JSONResponse({"error": "Invalid JSON"}, status_code=400)
        )

    raw_values = body.get("issues", [])
    if not raw_values or not isinstance(raw_values, list):
        return IssueNumbersPayload(
            error_response=JSONResponse(
                {"error": "issues must be a non-empty list"},
                status_code=400,
            )
        )
    normalized = normalize_issue_numbers(raw_values)
    if not normalized:
        return IssueNumbersPayload(
            body=body,
            error_response=JSONResponse(
                {"error": "issues must contain at least one positive issue number"},
                status_code=400,
            ),
        )
    return IssueNumbersPayload(issue_numbers=normalized, body=body)


def normalize_issue_numbers(values: list[Any]) -> list[int]:
    """Deduplicate positive issue numbers from ints or decimal strings."""
    numbers: list[int] = []
    seen: set[int] = set()
    for value in values:
        parsed: int | None = None
        if isinstance(value, bool):
            parsed = None
        elif isinstance(value, int):
            parsed = value
        elif isinstance(value, str) and value.strip().isdigit():
            parsed = int(value.strip())
        if parsed is None or parsed <= 0 or parsed in seen:
            continue
        numbers.append(parsed)
        seen.add(parsed)
    return numbers
