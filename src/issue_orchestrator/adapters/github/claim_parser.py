"""Parser for claim YAML blocks in GitHub-backed claim records.

This module handles parsing and formatting of claim metadata stored in
GitHub claim comments or Git ref commit messages for multi-orchestrator
coordination.

Claim format:
```io-claim
lease_id: abc123-1705412345
claimant: orchestrator-a
started_at: 2024-01-16T12:00:00
expires_at: 2024-01-16T12:15:00
priority: 1705412345000
```
"""

import logging
import re
from datetime import datetime
from typing import Any

import yaml

from ...domain.claim import Claim

logger = logging.getLogger(__name__)

# Regex to extract io-claim fenced blocks from claim record text
# Matches ```io-claim ... ``` blocks (case-insensitive, multiline)
CLAIM_BLOCK_PATTERN = re.compile(
    r"```io-claim\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)

# Required fields in a claim YAML block
REQUIRED_CLAIM_FIELDS = frozenset(
    ["lease_id", "claimant", "started_at", "expires_at", "priority"]
)

# ISO format for datetime serialization
DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


def parse_claim_comment(body: str, issue_number: int = 0) -> Claim | None:
    """Parse a claim from text containing a claim block.

    Extracts the io-claim YAML block and parses it into a Claim object.
    If multiple claim blocks exist, uses the last one (most recent edit).

    Args:
        body: The full claim record text.
        issue_number: The issue number (for the Claim object).

    Returns:
        A Claim object if parsing succeeded, None if no valid claim found.
    """
    matches = CLAIM_BLOCK_PATTERN.findall(body)
    if not matches:
        return None

    # Use the last match (in case of edits/multiple blocks)
    yaml_content = matches[-1]

    try:
        data = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        logger.debug("Failed to parse claim YAML: %s", e)
        return None

    if not isinstance(data, dict):
        logger.debug("Claim YAML is not a dict: %s", type(data))
        return None

    return _dict_to_claim(data, issue_number)


def _dict_to_claim(data: dict[str, Any], issue_number: int) -> Claim | None:
    """Convert a parsed YAML dict to a Claim object.

    Args:
        data: The parsed YAML data.
        issue_number: The issue number for the Claim.

    Returns:
        A Claim object if all required fields are valid, None otherwise.
    """
    # Check required fields
    missing = REQUIRED_CLAIM_FIELDS - set(data.keys())
    if missing:
        logger.debug("Claim missing required fields: %s", missing)
        return None

    try:
        # Parse datetime fields
        started_at = _parse_datetime(data["started_at"])
        expires_at = _parse_datetime(data["expires_at"])

        if started_at is None or expires_at is None:
            logger.debug("Invalid datetime in claim")
            return None

        # Parse priority (must be int)
        priority = int(data["priority"])

        return Claim(
            lease_id=str(data["lease_id"]),
            claimant=str(data["claimant"]),
            issue_number=issue_number,
            started_at=started_at,
            expires_at=expires_at,
            priority=priority,
        )
    except (ValueError, TypeError) as e:
        logger.debug("Failed to construct Claim: %s", e)
        return None


def _parse_datetime(value: Any) -> datetime | None:
    """Parse a datetime from various formats.

    Supports:
    - datetime objects (passthrough)
    - ISO format strings
    - Strings with format YYYY-MM-DDTHH:MM:SS

    Args:
        value: The value to parse.

    Returns:
        A datetime object or None if parsing failed.
    """
    if isinstance(value, datetime):
        return value

    if not isinstance(value, str):
        return None

    # Try ISO format first
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass

    # Try our standard format
    try:
        return datetime.strptime(value, DATETIME_FORMAT)
    except ValueError:
        pass

    return None


def format_claim_comment(claim: Claim) -> str:
    """Format a claim as a comment body with YAML block.

    Args:
        claim: The Claim to format.

    Returns:
        A string suitable for posting as a GitHub comment.
    """
    yaml_data = {
        "lease_id": claim.lease_id,
        "claimant": claim.claimant,
        "started_at": claim.started_at.strftime(DATETIME_FORMAT),
        "expires_at": claim.expires_at.strftime(DATETIME_FORMAT),
        "priority": claim.priority,
    }

    yaml_str = yaml.safe_dump(yaml_data, sort_keys=False, default_flow_style=False)

    return f"```io-claim\n{yaml_str.strip()}\n```"


def extract_all_claims(comments: list[dict[str, Any]], issue_number: int) -> list[Claim]:
    """Extract all valid claims from a list of comments.

    Args:
        comments: List of comment dicts with "body" field.
        issue_number: The issue number for the Claims.

    Returns:
        List of valid Claim objects found in the comments.
    """
    claims = []
    for comment in comments:
        body = comment.get("body", "")
        if not body:
            continue

        claim = parse_claim_comment(body, issue_number)
        if claim is not None:
            claims.append(claim)

    return claims
