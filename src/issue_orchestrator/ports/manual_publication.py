"""Preparation authority and manual execution are separate behavior boundaries."""

from collections.abc import Callable
from typing import Protocol

from ..domain.completion_processing import ProcessingResult
from ..domain.manual_publication import ManualPublicationResult, PreparedManualPublication
from ..domain.publish_retry import PublishRetryLocators
from ..domain.validated_head_publication import PublishValidatedHeadOutcome


class ManualPublicationPreparation(Protocol):
    def prepare_manual_publication(
        self, locators: PublishRetryLocators, issue_title: str,
    ) -> PreparedManualPublication | ProcessingResult: ...

    def settle_manual_publication(
        self, prepared: PreparedManualPublication, publication: PublishValidatedHeadOutcome,
    ) -> ProcessingResult: ...


class ManualPublisher(Protocol):
    def publish(
        self, locators: PublishRetryLocators, issue_title: str,
        is_current: Callable[[], bool],
    ) -> ManualPublicationResult: ...
