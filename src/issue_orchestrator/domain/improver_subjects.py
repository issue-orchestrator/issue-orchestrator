"""What a tech-lead record is about, as the improver reads it (#7490).

One rule, shared by the staging that assembles ``blocked-items.json`` and the
validator that checks a finding's citations, so both say "this decision, case
file or run is about issue #n" the same way:

* a charter decision is about the issue it targets, or its anchor issue when
  it targets none;
* a text the tech lead wrote is about every issue it names as ``#<n>`` (not
  ``owner/repo#<n>``, which is another repository's).
"""

from __future__ import annotations

import re
from collections.abc import Collection


def decision_issue(target_number: int | None, anchor_issue_number: int) -> int:
    """The issue a charter decision is about."""
    return target_number if target_number is not None else anchor_issue_number


def mentions_issue(text: str, numbers: Collection[int]) -> bool:
    """Whether ``text`` names any of ``numbers`` as ``#<n>`` in this repository."""
    if not numbers:
        return False
    pattern = r"(?<![\w/])#(?:" + "|".join(str(n) for n in sorted(set(numbers))) + r")\b"
    return re.search(pattern, text) is not None


__all__ = ["decision_issue", "mentions_issue"]
