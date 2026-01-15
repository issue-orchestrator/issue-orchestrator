"""Shared branch naming utilities."""

from __future__ import annotations

import re
import unicodedata


BRANCH_ISSUE_PATTERN = re.compile(r"^(\d+)-")


def slugify(text: str, max_length: int = 40) -> str:
    """Convert text to a URL-friendly slug."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    if len(text) > max_length:
        text = text[:max_length].rstrip("-")
    return text


def generate_branch_name(issue_number: int, issue_title: str) -> str:
    """Generate a branch name from issue number and title."""
    slug = slugify(issue_title, max_length=50)
    return f"{issue_number}-{slug}"


def extract_issue_number_from_branch(branch_name: str) -> int | None:
    """Extract issue number from a branch name."""
    match = BRANCH_ISSUE_PATTERN.match(branch_name)
    if match:
        return int(match.group(1))
    return None
