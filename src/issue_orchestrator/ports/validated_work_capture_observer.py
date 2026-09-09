"""Fresh remote observation required before automatic retained-work publication."""

from typing import Protocol

from ..domain.validated_work_capture import (
    ValidatedWorkRemoteFacts,
    ValidatedWorkRemoteRequest,
)
from ..domain.publication_remote import PublicationRemoteError


class ValidatedWorkCaptureObserver(Protocol):
    def observe(self, request: ValidatedWorkRemoteRequest) -> ValidatedWorkRemoteFacts:
        """Read the complete uncached branch and open-PR facts or raise."""
        ...


class UnavailableValidatedWorkCaptureObserver:
    def observe(self, request: ValidatedWorkRemoteRequest) -> ValidatedWorkRemoteFacts:
        raise PublicationRemoteError("validated-work remote observation is unavailable")
