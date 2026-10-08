"""The findings document inside an agent's final message, fail-closed (#8001)."""

from __future__ import annotations

import json

import pytest

from issue_orchestrator.domain.improver_answer import AnswerNotExtractable, extract_findings_answer

DOC = {"schema_version": 6, "findings": [{"id": "a", "why": "a {braced} phrase"}], "trend": {"notes": "}"}}
BODY = json.dumps(DOC)


@pytest.mark.parametrize(
    "message",
    [
        BODY,
        f"  {BODY}\n",
        f"```json\n{BODY}\n```",
        json.dumps(DOC, indent=2),
    ],
)
def test_a_message_that_is_the_document_is_taken_whole(message: str) -> None:
    extracted = extract_findings_answer(message)

    assert json.loads(extracted.text) == DOC and extracted.discarded == ""


@pytest.mark.parametrize(
    ("message", "prose"),
    [
        # The first real porchpin run's heat 2 (2026-10-08): a sentence, then the file.
        (f"The latest exam run failed case H, so the exam trend is down. Below is the findings file.\n\n{BODY}",
         "The latest exam run failed case H, so the exam trend is down. Below is the findings file."),
        (f"Here it is:\n```json\n{BODY}\n```\nDone, as asked.", "Done, as asked."),
    ],
)
def test_prose_around_one_document_is_discarded_and_reported(message: str, prose: str) -> None:
    extracted = extract_findings_answer(message)

    assert json.loads(extracted.text) == DOC
    assert prose in extracted.discarded and "schema_version" not in extracted.discarded


@pytest.mark.parametrize(
    ("message", "why"),
    [
        ("I could not finish the audit.", "0 JSON object"),
        (f"First draft: {BODY}\nFinal: {BODY}", "2 JSON object"),
        (f"Below is the findings file.\n{BODY[:-40]}", "does not parse"),  # cut short
        (f"Below: {BODY}\nand a stray {{\"half\": ", "does not parse"),
        (f"Below: {BODY[:20]} oops {BODY}", "does not parse"),
        # An object inside a larger value is not the answer.
        (f"Here is the answer: [{BODY}]", "JSON structure"),
        (f"Answers: [{BODY}, 3]", "JSON structure"),
        # A second object begun and cut off at its brace.
        (f"{BODY}\nAdditional: {{", "JSON structure"),
        (f"Note {{see below}}: {BODY}", "JSON structure"),
    ],
)
def test_no_document_two_documents_or_a_broken_one_refuse_the_answer(message: str, why: str) -> None:
    with pytest.raises(AnswerNotExtractable, match=why):
        extract_findings_answer(message)
