"""Unit tests for the exchange-respond CLI payload construction.

Network delivery is exercised end-to-end by the integration suite; here we
pin the payload shaping and validation, which is the part that has to match
the orchestrator's verdict parser exactly.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.entrypoints.cli_tools.exchange_respond import (
    build_parser,
    build_payload,
)


def _parse(argv: list[str]):
    return build_parser().parse_args(argv)


class TestBuildPayload:
    def test_minimal_ok(self) -> None:
        payload = build_payload(_parse(["ok", "--text", "Looks good."]))
        assert payload == {"response_type": "ok", "response_text": "Looks good."}

    def test_getting_closer_flag(self) -> None:
        payload = build_payload(
            _parse(["changes_requested", "--text", "Fix X.", "--getting-closer"])
        )
        assert payload["getting_closer"] is True

    def test_not_getting_closer_flag(self) -> None:
        payload = build_payload(
            _parse(["disagree", "--text", "Wrong.", "--not-getting-closer"])
        )
        assert payload["getting_closer"] is False

    def test_decision_json_merged_under_decision_key(self) -> None:
        payload = build_payload(
            _parse(
                [
                    "changes_requested",
                    "--text",
                    "See F1.",
                    "--decision-json",
                    '{"verdict":"changes_requested","risk":"medium"}',
                ]
            )
        )
        assert payload["decision"] == {
            "verdict": "changes_requested",
            "risk": "medium",
        }

    def test_full_json_overrides_positional_form(self) -> None:
        payload = build_payload(
            _parse(["--json", '{"response_type":"ok","response_text":"hi"}'])
        )
        assert payload == {"response_type": "ok", "response_text": "hi"}

    def test_text_required_without_full_json(self) -> None:
        with pytest.raises(ValueError, match="--text is required"):
            build_payload(_parse(["ok"]))

    def test_response_type_required_without_full_json(self) -> None:
        with pytest.raises(ValueError, match="response_type is required"):
            build_payload(_parse(["--text", "orphan text"]))

    def test_full_json_must_be_object(self) -> None:
        with pytest.raises(ValueError, match="must be a JSON object"):
            build_payload(_parse(["--json", "[1, 2, 3]"]))

    def test_full_json_must_be_valid_json(self) -> None:
        with pytest.raises(ValueError, match="not valid JSON"):
            build_payload(_parse(["--json", "{not json}"]))

    def test_decision_json_must_be_object(self) -> None:
        with pytest.raises(ValueError, match="--decision-json must be a JSON object"):
            build_payload(
                _parse(["ok", "--text", "t", "--decision-json", '"a string"'])
            )
