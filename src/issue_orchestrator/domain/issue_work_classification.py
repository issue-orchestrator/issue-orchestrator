"""Work/evidence identity survives the lifetime of a fetched queue snapshot."""

from collections.abc import Iterable
from enum import StrEnum

from .tech_lead_session import is_tech_lead_observation_label


class IssueWorkClassification(StrEnum):
    WORK = "work"
    EVIDENCE = "evidence"


def classify_issue_work(labels: Iterable[str]) -> IssueWorkClassification:
    """Only the explicit domain marker classifies an issue as evidence."""
    return (IssueWorkClassification.EVIDENCE
            if any(is_tech_lead_observation_label(label) for label in labels)
            else IssueWorkClassification.WORK)
