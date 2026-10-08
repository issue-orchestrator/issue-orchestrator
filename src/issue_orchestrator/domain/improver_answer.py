"""The findings document inside an agent's final message, fail-closed (#8001).

The prompt asks for the findings JSON alone, but a model sometimes writes a
sentence first ("Below is the findings file...", the first real porchpin run,
2026-10-08). The answer is:

* the whole message, when it is one JSON document (or one fenced block);
* otherwise the ONE top-level JSON object in it, the prose around it
  discarded (and reported, so it is logged);
* otherwise nothing: no object, two objects, or an object that does not
  parse (truncated, or broken) refuse the answer. Guessing between two
  documents, or reading a fragment of a cut-off one, never happens.

What is extracted is still validated in full; this only finds it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_FENCED = re.compile(r"\A```(?:json)?\s*\n(?P<body>.*)\n```\Z", re.DOTALL)
#: Where a JSON object (not a prose brace) begins: ``{`` then a key or ``}``.
_OBJECT_START = re.compile(r"\{\s*(?:\"|\})")


class AnswerNotExtractable(ValueError):
    """The message holds no single findings document."""


@dataclass(frozen=True)
class ExtractedAnswer:
    #: The findings document's text, ending with a newline.
    text: str
    #: The prose around it that was discarded ("" when there was none).
    discarded: str


def extract_findings_answer(message: str) -> ExtractedAnswer:
    stripped = message.strip()
    fenced = _FENCED.match(stripped)
    whole = fenced["body"].strip() if fenced else stripped
    try:
        json.loads(whole)
    except json.JSONDecodeError:
        pass
    else:
        return ExtractedAnswer(whole + "\n", "")
    objects = _top_level_objects(stripped)
    if len(objects) != 1:
        raise AnswerNotExtractable(
            f"the final message holds {len(objects)} JSON object(s) beside prose, not exactly one"
        )
    start, end = objects[0]
    discarded = f"{stripped[:start]} {stripped[end:]}".strip()
    return ExtractedAnswer(stripped[start:end] + "\n", discarded)


def _top_level_objects(text: str) -> list[tuple[int, int]]:
    """Every top-level JSON object's span; raises at one that begins and
    does not parse (a truncated or broken document is never skipped over)."""
    decoder = json.JSONDecoder()
    spans: list[tuple[int, int]] = []
    position = 0
    while (match := _OBJECT_START.search(text, position)) is not None:
        start = match.start()
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError as error:
            raise AnswerNotExtractable(
                f"the final message has a JSON object at character {start} that does not parse: {error.msg}"
            ) from error
        if isinstance(value, dict) and value:
            spans.append((start, end))
        position = end
    return spans


__all__ = ["AnswerNotExtractable", "ExtractedAnswer", "extract_findings_answer"]
