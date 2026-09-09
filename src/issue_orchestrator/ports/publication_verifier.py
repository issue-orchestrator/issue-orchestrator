"""Uncached publication preflight and final confirmation; never remote writes."""

from typing import Protocol

from ..domain.publication_verification import PublicationVerification
from ..domain.validated_head_publication import PublishValidatedHeadCommand
from ..domain.validated_work import DispositionPhase


class PublicationVerifier(Protocol):
    def before_publication(self, command: PublishValidatedHeadCommand,
                           phase: DispositionPhase) -> PublicationVerification: ...

    def confirm_target(self, command: PublishValidatedHeadCommand) -> PublicationVerification: ...
