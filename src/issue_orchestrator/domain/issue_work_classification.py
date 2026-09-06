"""Work/evidence identity survives the lifetime of a fetched queue snapshot."""

from collections.abc import Iterable, Mapping
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


def resolve_work_classifications(
    *,
    historical_labels: Iterable[tuple[int, Iterable[str]]],
    active_labels: Iterable[tuple[int, Iterable[str]]],
    cached_labels: Iterable[tuple[int, Iterable[str]]],
    retained: Mapping[int, IssueWorkClassification],
    observed_labels: Iterable[tuple[int, Iterable[str]]] = (),
) -> dict[int, IssueWorkClassification]:
    """Resolve identity with one precedence rule for retention and projection.

    History seeds identity, restored active issues supersede history, and queue
    facts supersede restored sessions. Durable identity outranks all restored
    labels; only explicit fresh observations can override durable identity.
    """
    classifications = {}
    for number, labels in historical_labels:
        if values := tuple(labels):
            classifications[number] = classify_issue_work(values)
    for source in (active_labels, cached_labels):
        classifications.update({number: classify_issue_work(labels) for number, labels in source})
    classifications.update(retained)
    classifications.update({number: classify_issue_work(labels) for number, labels in observed_labels})
    return classifications
