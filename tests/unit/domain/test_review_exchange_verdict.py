"""Unit tests for the ExchangeVerdict transport contract."""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.review_exchange_verdict import ExchangeVerdict


class TestFromWire:
    def test_full_payload_round_trips(self) -> None:
        raw = {
            "response_type": "changes_requested",
            "response_text": "See F1.",
            "getting_closer": True,
            "decision": {"verdict": "changes_requested", "risk": "medium"},
        }
        verdict = ExchangeVerdict.from_wire(raw)
        assert verdict.response_type == "changes_requested"
        assert verdict.getting_closer is True
        assert verdict.decision == {"verdict": "changes_requested", "risk": "medium"}
        assert dict(verdict.to_wire()) == raw

    def test_non_mapping_payload_is_rejected(self) -> None:
        for bad in (None, [1, 2], "ok", 5):
            with pytest.raises(ValueError, match="must be a JSON object"):
                ExchangeVerdict.from_wire(bad)  # type: ignore[arg-type]

    def test_wrong_typed_fields_become_none_and_are_omitted(self) -> None:
        # Preserves the legacy file-channel outcome: a missing/odd response_type
        # serialises back to absent, so the orchestrator parser still reports
        # missing_response_type rather than seeing a sentinel.
        verdict = ExchangeVerdict.from_wire(
            {"response_type": 123, "getting_closer": "yes", "decision": ["x"]}
        )
        assert verdict.response_type is None
        assert verdict.getting_closer is None
        assert verdict.decision is None
        assert dict(verdict.to_wire()) == {}

    def test_unknown_response_type_is_carried_through(self) -> None:
        # The endpoint does not pre-empt semantic validation; an unknown
        # response_type reaches the orchestrator parser verbatim.
        verdict = ExchangeVerdict.from_wire(
            {"response_type": "wat", "response_text": "rogue"}
        )
        assert dict(verdict.to_wire()) == {
            "response_type": "wat",
            "response_text": "rogue",
        }


class TestToWire:
    def test_omits_unset_optionals(self) -> None:
        verdict = ExchangeVerdict(
            response_type="ok",
            response_text="done",
            getting_closer=None,
            decision=None,
        )
        assert dict(verdict.to_wire()) == {
            "response_type": "ok",
            "response_text": "done",
        }

    def test_getting_closer_false_is_preserved(self) -> None:
        verdict = ExchangeVerdict(
            response_type="disagree",
            response_text="no",
            getting_closer=False,
            decision=None,
        )
        assert dict(verdict.to_wire())["getting_closer"] is False
