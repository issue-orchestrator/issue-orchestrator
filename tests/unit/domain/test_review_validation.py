import json

import pytest

from issue_orchestrator.domain.review_validation import ReviewValidationEvidence


def test_validation_evidence_binds_bytes_to_head_and_outcome() -> None:
    payload = {"passed": True, "head_sha": "head-a"}
    evidence = ReviewValidationEvidence.from_mapping(payload)

    assert json.loads(evidence.result_bytes) == payload
    assert evidence.head_sha == "head-a"
    assert evidence.passed is True


@pytest.mark.parametrize(
    ("head_sha", "passed", "match"),
    [("head-b", True, "head differs"), ("head-a", False, "outcome differs")],
)
def test_validation_evidence_rejects_identity_outside_its_bytes(
    head_sha: str, passed: bool, match: str,
) -> None:
    payload = json.dumps({"passed": True, "head_sha": "head-a"}).encode()

    with pytest.raises(ValueError, match=match):
        ReviewValidationEvidence(payload, head_sha, passed)
