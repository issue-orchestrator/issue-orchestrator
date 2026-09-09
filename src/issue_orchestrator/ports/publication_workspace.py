"""Verified preparation assets owned under the caller's disposition issue gate."""

from typing import Protocol

from ..domain.publication_workspace import PublicationWorkspace
from ..domain.validated_work_store import EvidenceAdmission


class PublicationWorkspaces(Protocol):
    def prepare(self, admission: EvidenceAdmission) -> PublicationWorkspace:
        """Reconstruct exact work from escrow; refuse unknown or modified paths.

        Caller holds the disposition issue gate for this allocation operation.
        The execution lease and durable claim protect subsequent publication;
        release the issue gate before publication subprocesses or finalization.
        This creates no coding-session allocation.
        """
        ...

    def release(self, admission: EvidenceAdmission) -> None:
        """Remove only verified publication-owned assets, under the same gate."""
        ...
