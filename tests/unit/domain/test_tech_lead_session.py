"""Tests for the tech_lead session assignment domain type (ADR-0031)."""

import json
from pathlib import Path

import pytest

from issue_orchestrator.domain.tech_lead_session import (
    TECH_LEAD_ASSIGNMENT_FILENAME,
    TECH_LEAD_OBSERVATION_LABEL,
    TechLeadAssignment,
    TechLeadSessionFlavor,
    require_case_file_observation_label,
)


class TestCaseFileObservationLabelInvariant:
    """The domain owns the pattern case-file label invariant (#6781)."""

    def test_accepts_labels_carrying_the_observation_label(self) -> None:
        require_case_file_observation_label(
            ("agent:tech-lead", TECH_LEAD_OBSERVATION_LABEL, "area:db")
        )  # does not raise

    def test_rejects_labels_missing_the_observation_label(self) -> None:
        with pytest.raises(ValueError, match="observation label"):
            require_case_file_observation_label(("agent:tech-lead", "area:db"))


class TestTechLeadAssignmentRoundTrip:
    def test_batch_review_round_trips_through_file(self, tmp_path: Path) -> None:
        assignment = TechLeadAssignment(flavor=TechLeadSessionFlavor.BATCH_REVIEW)
        path = tmp_path / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME

        assignment.write(path)

        assert TechLeadAssignment.read(path) == assignment

    def test_failure_investigation_round_trips_focus_fields(
        self, tmp_path: Path
    ) -> None:
        assignment = TechLeadAssignment(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=4321,
            focus_reason="Investigate: session timed out",
        )
        path = tmp_path / TECH_LEAD_ASSIGNMENT_FILENAME

        assignment.write(path)
        loaded = TechLeadAssignment.read(path)

        assert loaded == assignment
        assert loaded.focus_issue_number == 4321
        assert loaded.focus_reason == "Investigate: session timed out"

    def test_health_review_round_trips_through_file(self, tmp_path: Path) -> None:
        """Health reviews carry no focus fields — like batch (ADR-0031 §4)."""
        assignment = TechLeadAssignment(flavor=TechLeadSessionFlavor.HEALTH_REVIEW)
        path = tmp_path / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME

        assignment.write(path)
        loaded = TechLeadAssignment.read(path)

        assert loaded == assignment
        assert loaded.focus_issue_number is None
        assert loaded.focus_reason == ""

    def test_write_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / TECH_LEAD_ASSIGNMENT_FILENAME

        TechLeadAssignment(flavor=TechLeadSessionFlavor.BATCH_REVIEW).write(path)

        assert path.exists()

    def test_serialized_form_is_stable(self) -> None:
        assignment = TechLeadAssignment(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=7,
            focus_reason="broken",
        )

        assert assignment.to_dict() == {
            "schema_version": 1,
            "flavor": "failure_investigation",
            "focus_issue_number": 7,
            "focus_reason": "broken",
        }


class TestTechLeadAssignmentValidation:
    def test_unknown_flavor_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="Unknown tech_lead assignment flavor"):
            TechLeadAssignment.from_dict(
                {"schema_version": 1, "flavor": "board_walkthrough"}
            )

    def test_missing_flavor_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="Unknown tech_lead assignment flavor"):
            TechLeadAssignment.from_dict({"schema_version": 1})

    def test_bad_schema_version_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="schema_version"):
            TechLeadAssignment.from_dict(
                {"schema_version": 99, "flavor": "batch_review"}
            )

    def test_non_int_schema_version_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="schema_version"):
            TechLeadAssignment.from_dict(
                {"schema_version": "1", "flavor": "batch_review"}
            )

    def test_failure_flavor_requires_focus_issue_number(self) -> None:
        with pytest.raises(ValueError, match="focus_issue_number"):
            TechLeadAssignment(flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION)

    def test_failure_flavor_requires_focus_issue_number_from_dict(self) -> None:
        with pytest.raises(ValueError, match="focus_issue_number"):
            TechLeadAssignment.from_dict(
                {"schema_version": 1, "flavor": "failure_investigation"}
            )

    def test_non_int_focus_issue_number_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="focus_issue_number"):
            TechLeadAssignment.from_dict(
                {
                    "schema_version": 1,
                    "flavor": "failure_investigation",
                    "focus_issue_number": "42",
                }
            )

    def test_non_string_focus_reason_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="focus_reason"):
            TechLeadAssignment.from_dict(
                {
                    "schema_version": 1,
                    "flavor": "failure_investigation",
                    "focus_issue_number": 42,
                    "focus_reason": 3,
                }
            )

    def test_malformed_json_raises_from_read(self, tmp_path: Path) -> None:
        path = tmp_path / TECH_LEAD_ASSIGNMENT_FILENAME
        path.write_text("{not json")

        with pytest.raises(json.JSONDecodeError):
            TechLeadAssignment.read(path)
