"""Concrete external boundaries for validated-work composition."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..adapters.github.http_client import GitHubHttpClient
from ..adapters.github.publication_remote import GitHubPublicationRemote
from ..adapters.github.recovery_issue_reader import GitHubRecoveryIssueReader
from ..adapters.issue_disposition_gate import FileIssueDispositionMutationGate
from ..ports.issue_disposition_gate import IssueDispositionMutationGate
from ..ports.publication_remote import PublicationRemote
from ..ports.recovery_issue_reader import RecoveryIssueReader


@dataclass(frozen=True, slots=True)
class ValidatedWorkExternalAdapters:
    gate: IssueDispositionMutationGate
    remote: PublicationRemote
    issues: RecoveryIssueReader


class ValidatedWorkRepositoryHost(Protocol):
    @property
    def http_client(self) -> GitHubHttpClient: ...


def build_validated_work_external_adapters(
    *, repo_root: Path, repo_slug: str, repository_host: ValidatedWorkRepositoryHost
) -> ValidatedWorkExternalAdapters:
    """Bind recovery ports to the one production GitHub adapter."""
    client = repository_host.http_client
    return ValidatedWorkExternalAdapters(
        gate=FileIssueDispositionMutationGate(repo_root),
        remote=GitHubPublicationRemote(client, repo_slug=repo_slug),
        issues=GitHubRecoveryIssueReader(client, repo_slug=repo_slug),
    )
